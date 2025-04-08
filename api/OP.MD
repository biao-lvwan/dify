# 修改全局源
# C:\Users\Administrator\AppData\Roaming\pip
# 安装 pypoetry
powershell "(Invoke-WebRequest -Uri https://install.python-poetry.org -UseBasicParsing).Content | py -"
# 设置pypoetry的环境变量
C:\Users\Administrator\AppData\Roaming\pypoetry\venv\Scripts
poetry --version

poetry env use 3.10

pip install poetry --index-url https://mirrors.aliyun.com/pypi/simple

pip install -r requirements.txt

# 查看第三方包
pip freeze
# 编译镜像
docker build -t langgenius/dify-api:latest .

# conda env list
# 切换环境
# conda activate dify 
