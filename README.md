# 交接台秤的去皮验证与分装净重

这个项目保存骑手服务站的合成设备资料和本地回放程序。设备能力、时钟和物理证据约定见 [设备资料](protocol.md)。

使用 Python 3.11 标准库。在项目根目录运行 python3 bench.py 查看当前记录，运行 python3 -m unittest discover -s tests -v 核对资料以及本机 HTTP 消息。python3 bench.py --listen 8765 启动回放端，--scene 可另选同格式的观测。回放不接管物理设备。
