# 选款网站部署说明

给部署的同事看：照着做就能把网站放到公网上。整份程序只有这一个文件夹，不需要仓库里别的东西。

## 1. 这是什么

达人（博主）打开运营发给她的专属链接，在手机上挑样品帽子（款式、颜色、尺码），填收货信息，提交。

```
达人的手机 ──HTTPS──▶ 选款网站（这台服务器） ◀──HTTPS── 运营的 Mac（定时来取）
```

- 网站只等别人来访问，**从不主动连接任何地方**：运营的 Mac 在内网里，由 Mac 定时来网站取新提交的选款（管理接口，要管理口令）。
- 商品目录、图片、专属链接都由 Mac 通过管理接口放上来，服务器上不用手动维护。
- 数据都在一个 SQLite 文件里（里面有达人的收货地址和手机号，按敏感数据对待）。Mac 取走以后，网站只留打码的残余（名字第一个字、138****0000、省市）；超过保留天数（默认 90 天）还没处理的也会自动清成这样。

## 2. 需要什么

- 一台 Linux 服务器（1 核 1G 足够），Python 3.9 或更新版本，**或者** Docker。
- 不用装任何 Python 包（只用标准库）。磁盘约 50 MB。
- 入站：80 和 443 端口（给 HTTPS）。出站：不需要（Caddy 申请证书时要访问 Let's Encrypt）。
- 一个域名，最好是公司已有备案域名下的子域名，比如 `xuan.公司域名.com`（原因见第 6 节）。

## 3. 设置（环境变量）

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `SAMPLE_SITE_ADMIN_TOKEN` | 是（或用下一项） | — | 管理口令，至少 32 个字符，不能有空格。少于 32 位拒绝启动 |
| `SAMPLE_SITE_ADMIN_TOKEN_FILE` | | — | 不想放在环境变量里时，把口令写在这个文件里 |
| `SAMPLE_SITE_PUBLIC_URL` | 建议填 | 按请求的域名拼 | 网站的公网地址，如 `https://xuan.example.com`；是 https 时自动发 HSTS |
| `SAMPLE_SITE_DB` | | `./data/sample_site.db` | 数据库文件（自动建，权限 600） |
| `SAMPLE_SITE_HOST` | | `127.0.0.1` | 监听地址。前面有 Caddy/nginx 时保持默认 |
| `SAMPLE_SITE_PORT` | | `8080` | 监听端口 |
| `SAMPLE_SITE_TRUST_PROXY` | | `0` | 前面有反向代理时设 `1`（从 X-Forwarded-For 最后一跳取访客 IP，用来限流） |
| `SAMPLE_SITE_RETENTION_DAYS` | | `90` | 收货信息最多保留几天 |
| `SAMPLE_SITE_BRAND` | | `ACE7CN` | 页面顶上的品牌名 |

生成管理口令：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 4. 启动方式 A：直接用 Python（systemd）

```bash
sudo useradd --system --home /opt/sample_site --shell /usr/sbin/nologin samplesite
sudo cp -r sample_site /opt/sample_site          # 只拷这个文件夹
sudo mkdir -p /opt/sample_site/data && sudo chown samplesite: /opt/sample_site/data && sudo chmod 700 /opt/sample_site/data
sudo cp /opt/sample_site/site.env.example /etc/sample_site.env
sudo chmod 600 /etc/sample_site.env && sudo nano /etc/sample_site.env   # 填口令和公网地址
```

`/etc/systemd/system/sample_site.service`：

```ini
[Unit]
Description=ACE7CN sample picking site
After=network.target

[Service]
User=samplesite
Group=samplesite
WorkingDirectory=/opt/sample_site
EnvironmentFile=/etc/sample_site.env
Environment=SAMPLE_SITE_TRUST_PROXY=1
ExecStart=/usr/bin/python3 /opt/sample_site/app.py
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/sample_site/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now sample_site
sudo journalctl -u sample_site -f      # 看到 "sample site listening on http://127.0.0.1:8080" 就好了
```

本机试跑（不装服务）：`cd sample_site && SAMPLE_SITE_ADMIN_TOKEN=... python3 app.py`。

想用 gunicorn / waitress 也可以（不是必须）：在文件夹里 `gunicorn -w 2 -b 127.0.0.1:8080 wsgi:application`。**不要**打开它们的访问日志（`--access-logfile`），见第 9 节。

## 5. 启动方式 B：Docker

```bash
cd sample_site
cp docker-compose.example.yml docker-compose.yml
cp Caddyfile.example Caddyfile            # 把里面的 xuan.example.com 换成你的域名
cp site.env.example site.env && chmod 600 site.env   # 填口令和 https 公网地址
docker compose up -d --build
docker compose logs -f site
```

这样起来的是网站 + Caddy（自动申请和续期 HTTPS 证书）。数据在 Docker 卷 `site-data` 里（容器内 `/data/sample_site.db`），容器以非 root 用户运行。

只要网站、自己配反向代理时：`docker build -t sample-site . && docker run -d --restart unless-stopped --env-file site.env -e SAMPLE_SITE_TRUST_PROXY=1 -v sample-site-data:/data -p 127.0.0.1:8080:8080 sample-site`。

## 6. 域名和 HTTPS（重要）

- **必须 HTTPS**。网站自己只说 HTTP，前面放 Caddy（最省事，自动证书）或 nginx。
- nginx 的写法：

  ```nginx
  server {
      listen 443 ssl http2;
      server_name xuan.example.com;
      ssl_certificate     /etc/ssl/xuan/fullchain.pem;
      ssl_certificate_key /etc/ssl/xuan/privkey.pem;
      access_log off;                 # 链接里有达人的专属口令，不要记访问日志
      client_max_body_size 3m;
      location / {
          proxy_pass http://127.0.0.1:8080;
          proxy_set_header Host $host;
          proxy_set_header X-Forwarded-For $remote_addr;
          proxy_set_header X-Forwarded-Proto $scheme;
      }
  }
  server { listen 80; server_name xuan.example.com; return 301 https://$host$request_uri; }
  ```

- **服务器在中国大陆**：域名必须先完成 ICP 备案，否则云厂商会直接拦掉。最简单的是用公司已经备案的域名开一个子域名（如 `xuan.公司域名.com`），子域名不用另外备案。
- **微信内置浏览器**：没备案的、新注册的、被投诉或被标记过的域名，在微信里打开会被拦或弹「非官方网页」提示，达人很可能就不点了。所以：
  - 用备案过的老域名（公司主域名的子域名最好）；
  - 不要经过别的域名跳转（不用短链接、不套第三方跳转）；
  - **正式发给达人前，自己先在微信里把链接发给同事点开试一下**，能直接打开、没有警告页再用。
- **服务器在香港等境外**：不用备案，但大陆访问慢一些，也更容易碰到微信的拦截提示，不推荐。

## 7. 检查是否正常

```bash
curl https://xuan.example.com/healthz                       # 回 ok
curl -H "Authorization: Bearer 你的管理口令" https://xuan.example.com/admin/api/status
# 回 {"ok": true, "catalog": {...}, "links": {...}, "submissions": {...}, ...}
```

打开 `https://xuan.example.com/` 应该只看到一句「请使用运营发给您的专属链接」。

## 8. 部署好以后交给运营的

1. 网站的公网地址（如 `https://xuan.example.com`）；
2. 管理口令。

**口令走私聊（当面、加密通讯或口头），不要发在群里、邮件里或文档里。** 两位运营各自把它写进自己 Mac 的配置。

## 9. 日志和隐私

- 网站自己的日志不写收货人、电话、地址、抖音号，也不写链接里的口令（写成 `/s/…`），不写访客 IP。
- 反向代理的访问日志会把完整链接（含口令）写进去：Caddy 不要加 `log`，nginx 写 `access_log off;`。nginx 的 error log 在后端出错时也可能带上路径，出问题排查完可以清掉。
- 数据库文件里有收货信息，权限是 600，只给运行网站的用户读。

## 10. 备份

整个网站的数据就是那一个 SQLite 文件。

```bash
# 不停服务也能安全备份（需要 sqlite3 命令；Debian/Ubuntu: apt install sqlite3）
sqlite3 /opt/sample_site/data/sample_site.db ".backup '/root/backup/sample_site-$(date +%F).db'"
# Docker：docker compose stop site，再拷卷里的 sample_site.db，再 start
```

备份里同样有收货地址和手机号：放在只有管理员能读的地方，不用的旧备份删掉。不备份也行：Mac 那边取走以后就有完整记录了，网站上的只是暂存。

## 11. 换管理口令

1. 生成新口令（第 3 节的命令）；
2. 改掉 `/etc/sample_site.env`（或 `site.env`）里的 `SAMPLE_SITE_ADMIN_TOKEN`；
3. 重启：`sudo systemctl restart sample_site` 或 `docker compose up -d`；
4. 把新口令私聊给两位运营，让他们改进各自 Mac 的配置。

旧口令在重启那一刻就失效。达人手里的专属链接不受影响。

## 12. 升级

用新的 `sample_site` 文件夹覆盖程序文件（`data/` 目录不要动），然后重启服务（Docker 是 `docker compose up -d --build`）。数据库会自动沿用。
