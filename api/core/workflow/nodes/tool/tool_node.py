from collections.abc import Mapping, Sequence
from os import path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.callback_handler.workflow_tool_callback_handler import DifyWorkflowCallbackHandler
from core.file.models import File, FileTransferMethod, FileType
from core.tools.entities.tool_entities import ToolInvokeMessage, ToolParameter
from core.tools.tool_engine import ToolEngine
from core.tools.tool_manager import ToolManager
from core.tools.utils.message_transformer import ToolFileMessageTransformer
from core.workflow.entities.node_entities import NodeRunMetadataKey, NodeRunResult
from core.workflow.entities.variable_pool import VariablePool
from core.workflow.nodes.base import BaseNode
from core.workflow.nodes.enums import NodeType
from core.workflow.nodes.tool.entities import ToolNodeData
from core.workflow.utils.variable_template_parser import VariableTemplateParser
from extensions.ext_database import db
from models import ToolFile
from models.workflow import WorkflowNodeExecutionStatus


class ToolNode(BaseNode[ToolNodeData]):
    """
    Tool Node
    """

    _node_data_cls = ToolNodeData
    _node_type = NodeType.TOOL

    def _run(self) -> NodeRunResult:
        # fetch tool icon
        tool_info = {
            "provider_type": self.node_data.provider_type,
            "provider_id": self.node_data.provider_id,
        }

        # get tool runtime
        try:
            tool_runtime = ToolManager.get_workflow_tool_runtime(
                self.tenant_id, self.app_id, self.node_id, self.node_data, self.invoke_from
            )
        except Exception as e:
            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.FAILED,
                inputs={},
                metadata={
                    NodeRunMetadataKey.TOOL_INFO: tool_info,
                },
                error=f"Failed to get tool runtime: {str(e)}",
            )

        # get parameters
        tool_parameters = tool_runtime.parameters or []
        parameters = self._generate_parameters(
            tool_parameters=tool_parameters,
            variable_pool=self.graph_runtime_state.variable_pool,
            node_data=self.node_data,
        )
        parameters_for_log = self._generate_parameters(
            tool_parameters=tool_parameters,
            variable_pool=self.graph_runtime_state.variable_pool,
            node_data=self.node_data,
            for_log=True,
        )

        try:
            messages = ToolEngine.workflow_invoke(
                tool=tool_runtime,
                tool_parameters=parameters,
                user_id=self.user_id,
                workflow_tool_callback=DifyWorkflowCallbackHandler(),
                workflow_call_depth=self.workflow_call_depth,
                thread_pool_id=self.thread_pool_id,
            )
        except Exception as e:
            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.FAILED,
                inputs=parameters_for_log,
                metadata={
                    NodeRunMetadataKey.TOOL_INFO: tool_info,
                },
                error=f"Failed to invoke tool: {str(e)}",
            )

        # convert tool messages
        plain_text, files, json = self._convert_tool_messages(messages)

        return NodeRunResult(
            status=WorkflowNodeExecutionStatus.SUCCEEDED,
            outputs={
                "text": plain_text,
                "files": files,
                "json": json,
            },
            metadata={
                NodeRunMetadataKey.TOOL_INFO: tool_info,
            },
            inputs=parameters_for_log,
        )

    def _generate_parameters(
        self,
        *,
        tool_parameters: Sequence[ToolParameter],
        variable_pool: VariablePool,
        node_data: ToolNodeData,
        for_log: bool = False,
    ) -> Mapping[str, Any]:
        """
        Generate parameters based on the given tool parameters, variable pool, and node data.

        Args:
            tool_parameters (Sequence[ToolParameter]): The list of tool parameters.
            variable_pool (VariablePool): The variable pool containing the variables.
            node_data (ToolNodeData): The data associated with the tool node.

        Returns:
            Mapping[str, Any]: A dictionary containing the generated parameters.

        """
        tool_parameters_dictionary = {parameter.name: parameter for parameter in tool_parameters}

        result = {}
        for parameter_name in node_data.tool_parameters:
            parameter = tool_parameters_dictionary.get(parameter_name)
            if not parameter:
                result[parameter_name] = None
                continue
            tool_input = node_data.tool_parameters[parameter_name]
            if tool_input.type == "variable":
                variable = variable_pool.get(tool_input.value)
                if variable is None:
                    raise ValueError(f"variable {tool_input.value} not exists")
                parameter_value = variable.value
            elif tool_input.type in {"mixed", "constant"}:
                segment_group = variable_pool.convert_template(str(tool_input.value))
                parameter_value = segment_group.log if for_log else segment_group.text
            else:
                raise ValueError(f"unknown tool input type '{tool_input.type}'")
            result[parameter_name] = parameter_value

        return result

    def _convert_tool_messages(
        self,
        messages: list[ToolInvokeMessage],
    ):
        """
        Convert ToolInvokeMessages into tuple[plain_text, files]
        """
        # transform message and handle file storage
        messages = ToolFileMessageTransformer.transform_tool_invoke_messages(
            messages=messages,
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            conversation_id=None,
        )
        # extract plain text and files
        files = self._extract_tool_response_binary(messages)
        plain_text = self._extract_tool_response_text(messages)
        json = self._extract_tool_response_json(messages)

        return plain_text, files, json

    def _extract_tool_response_binary(self, tool_response: list[ToolInvokeMessage]) -> list[File]:
        """
        Extract tool response binary
        """
        result = []
        for response in tool_response:
            if response.type in {ToolInvokeMessage.MessageType.IMAGE_LINK, ToolInvokeMessage.MessageType.IMAGE}:
                url = str(response.message) if response.message else None
                ext = path.splitext(url)[1] if url else ".bin"
                tool_file_id = str(url).split("/")[-1].split(".")[0]
                transfer_method = response.meta.get("transfer_method", FileTransferMethod.TOOL_FILE)

                with Session(db.engine) as session:
                    stmt = select(ToolFile).where(ToolFile.id == tool_file_id)
                    tool_file = session.scalar(stmt)
                    if tool_file is None:
                        raise ValueError(f"tool file {tool_file_id} not exists")

                result.append(
                    File(
                        tenant_id=self.tenant_id,
                        type=FileType.IMAGE,
                        transfer_method=transfer_method,
                        remote_url=url,
                        related_id=tool_file.id,
                        filename=tool_file.name,
                        extension=ext,
                        mime_type=tool_file.mimetype,
                        size=tool_file.size,
                    )
                )
            elif response.type == ToolInvokeMessage.MessageType.BLOB:
                # get tool file id
                tool_file_id = str(response.message).split("/")[-1].split(".")[0]
                with Session(db.engine) as session:
                    stmt = select(ToolFile).where(ToolFile.id == tool_file_id)
                    tool_file = session.scalar(stmt)
                    if tool_file is None:
                        raise ValueError(f"tool file {tool_file_id} not exists")
                result.append(
                    File(
                        tenant_id=self.tenant_id,
                        type=FileType.IMAGE,
                        transfer_method=FileTransferMethod.TOOL_FILE,
                        related_id=tool_file.id,
                        filename=tool_file.name,
                        extension=path.splitext(response.save_as)[1],
                        mime_type=tool_file.mimetype,
                        size=tool_file.size,
                    )
                )
            elif response.type == ToolInvokeMessage.MessageType.LINK:
                url = str(response.message)
                transfer_method = FileTransferMethod.TOOL_FILE
                tool_file_id = url.split("/")[-1].split(".")[0]
                with Session(db.engine) as session:
                    stmt = select(ToolFile).where(ToolFile.id == tool_file_id)
                    tool_file = session.scalar(stmt)
                    if tool_file is None:
                        raise ValueError(f"tool file {tool_file_id} not exists")
                if "." in url:
                    extension = "." + url.split("/")[-1].split(".")[1]
                else:
                    extension = ".bin"
                file = File(
                    tenant_id=self.tenant_id,
                    type=FileType(response.save_as),
                    transfer_method=transfer_method,
                    remote_url=url,
                    filename=tool_file.name,
                    related_id=tool_file.id,
                    extension=extension,
                    mime_type=tool_file.mimetype,
                    size=tool_file.size,
                )
                result.append(file)

            elif response.type == ToolInvokeMessage.MessageType.FILE:
                assert response.meta is not None
                result.append(response.meta["file"])

        return result

    def _extract_tool_response_text(self, tool_response: list[ToolInvokeMessage]) -> str:
        """
        Extract tool response text
        """
        return "\n".join(
            [
                f"{message.message}"
                if message.type == ToolInvokeMessage.MessageType.TEXT
                else f"Link: {message.message}"
                if message.type == ToolInvokeMessage.MessageType.LINK
                else ""
                for message in tool_response
            ]
        )

    def _extract_tool_response_json(self, tool_response: list[ToolInvokeMessage]):
        return [message.message for message in tool_response if message.type == ToolInvokeMessage.MessageType.JSON]

    @classmethod
    def _extract_variable_selector_to_variable_mapping(
        cls,
        *,
        graph_config: Mapping[str, Any],
        node_id: str,
        node_data: ToolNodeData,
    ) -> Mapping[str, Sequence[str]]:
        """
        Extract variable selector to variable mapping
        :param graph_config: graph config
        :param node_id: node id
        :param node_data: node data
        :return:
        """
        result = {}
        for parameter_name in node_data.tool_parameters:
            input = node_data.tool_parameters[parameter_name]
            if input.type == "mixed":
                selectors = VariableTemplateParser(str(input.value)).extract_variable_selectors()
                for selector in selectors:
                    result[selector.variable] = selector.value_selector
            elif input.type == "variable":
                result[parameter_name] = input.value
            elif input.type == "constant":
                pass

        result = {node_id + "." + key: value for key, value in result.items()}

        return result
