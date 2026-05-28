# 远程访问部署指南

## 方案一：Cloudflare Tunnel（推荐，免费，快速）

1. 安装 cloudflared：
   ```
   winget install Cloudflare.cloudflared
   ```

2. 先启动价格监控服务（另开一个终端）：
   ```
   cd C:\Users\26859\price-monitor
   python server.py
   ```

3. 启动隧道：
   ```
   cloudflared tunnel --url http://localhost:5000
   ```

4. 终端会显示一个公网 URL 如 `https://xxx.trycloudflare.com`
5. 手机上打开这个 URL 即可随时查看

## 方案二：Render.com 云端部署（免费，永久在线）

1. 在 GitHub 上创建一个新仓库
2. 把本项目推送到仓库：
   ```
   cd C:\Users\26859\price-monitor
   git init
   git add .
   git commit -m "价格监控面板"
   git remote add origin <你的仓库地址>
   git push -u origin main
   ```
3. 登录 render.com，点 "New Web Service"
4. 连接 GitHub 仓库
5. Render 自动检测 Python 项目并部署
6. 获得永久域名如 `https://price-monitor.onrender.com`

## 方案三：Docker 部署

```bash
cd C:\Users\26859\price-monitor
docker build -t price-monitor .
docker run -d -p 5000:5000 --restart always price-monitor
```

## 方案四：路由器端口转发

1. 登录路由器管理页面
2. 设置端口转发：外网端口 5000 → 内网 192.168.x.x:5000
3. 如需固定域名，配置 DDNS（如花生壳）
