# QQ 监督员机器人 —— 容器部署（Koyeb / Render / Railway 等 PaaS 通用）
#
# 重要：不要用 requirements.txt 构建——那是给「本地跑大模型」用的
# （torch/transformers 好几个 GB，256MB 的 nano 实例装不下也跑不动）。
# 云端只跑「API 大脑」模式（QQ_BRAIN_MODEL=api），整条链路只有一个第三方依赖
# websocket-client，其余模块（qq_bot/qq_profiles/qq_scoreboard/qq_hr/qq_brain）
# 全是 Python 标准库。镜像实测约 140MB（slim 底座占大头），256MB 实例余量充足。

FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir websocket-client

# 只复制运行必需的源码；模型权重、日志、.env、数据文件一律不进镜像。
# 凭证通过 Koyeb 控制台的 Environment Variables 注入（详见 KOYEB_DEPLOY.md）。
COPY qq_receiver.py qq_bot.py qq_profiles.py qq_scoreboard.py qq_hr.py qq_brain.py ./

# Ports 留空：机器人是 QQ 网关的 WebSocket 客户端，不需要任何入站端口。
# -u：关闭 stdout 缓冲，Koyeb 控制台能实时看日志。
CMD ["python", "-u", "qq_receiver.py"]
