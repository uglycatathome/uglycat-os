# 可选：部署到自己的服务器（7×24 运行）

本地部署已经可用；只有当你的电脑经常关机/睡眠、又希望每天准点收到推送时，
才需要把它搬到一台常开的 Linux 服务器（你自己的云主机）。

## 1. 安装

```bash
git clone <你的仓库地址> ~/uglycat-os
cd ~/uglycat-os
./install/setup.sh
vi .env            # 填 LLM Key 与可选 SERVERCHAN_SENDKEY
./uglycat doctor --json
```

## 2. 用 systemd 常驻

本地机器用 `./uglycat install-schedule` 就够了；服务器上更推荐常驻进程，
避免依赖 cron 环境变量差异。

`/etc/systemd/system/uglycat.service`：

```ini
[Unit]
Description=UglyCat OS scheduler
After=network-online.target

[Service]
Type=simple
User=%i
WorkingDirectory=/home/%i/uglycat-os
ExecStart=/home/%i/uglycat-os/.venv/bin/python /home/%i/uglycat-os/run.py schedule daemon
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now uglycat
systemctl status uglycat
```

机器睡眠/停机期间也不会补跑，因此推荐同时保留 `catch-up` 思路：
开机后手动或由登录脚本执行一次 `./uglycat catch-up`。

## 3. 最小加固清单

- 只开放 22 端口，用 SSH Key 登录，禁用密码登录
- 定期 `apt upgrade`（或发行版等价命令）
- `.env` 权限 `chmod 600`
- 不把服务端口暴露到公网（本项目不提供 HTTP 服务）

## 4. 升级

```bash
cd ~/uglycat-os && git pull --ff-only
./install/setup.sh          # 幂等，会更新依赖
sudo systemctl restart uglycat
```
