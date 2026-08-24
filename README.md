# White-Label Add-ons for Home Assistant OS

两个 Supervisor 插件，把 HA 变成"隐形后端"，让你的品牌 App 独占终端用户交互：

| 插件 | 作用 | 端口 |
|------|------|------|
| **whitelabel_proxy** | nginx 反向代理：只放行 `/api/*` 和 `/api/websocket`，封掉所有网页界面 | 8080 |
| **whitelabel_pairing** | 配对服务：App 提交配对码 → 签发独立长期访问令牌 | 8099 |

> 已用模拟服务器端到端验证：配对 → 拿令牌 → 用令牌控制设备 → 收到状态事件。

```
ha-whitelabel-demo/addon-repo/
├── whitelabel_proxy/
│   ├── config.yaml
│   ├── Dockerfile
│   └── rootfs/etc/nginx/conf.d/default.conf
└── whitelabel_pairing/
    ├── config.yaml
    ├── Dockerfile
    └── rootfs/app/pairing.py
```

## 完整工作流

```
1) 出厂/首次上电：管理员在 HA 里创建主账户，生成一个"主长期令牌"，
   填入 whitelabel_pairing 的 master_token 选项；把配对码印在设备上。

2) 用户拿到设备 → 打开你的品牌 App → 输入设备上的配对码。
   App POST http://<网关IP>:8099/pair  {"pairing_code":"...","client_name":"MyApp"}
   ↓
   配对插件用主令牌连接 HA 的 WebSocket，调用 auth/long_lived_access_token
   签发一个**该设备专属**的长期令牌，返回给 App。

3) App 用这个令牌连接控制通道（走反向代理，UI 被封死）：
   ws://<网关IP>:8080/api/websocket   ←  认证、读状态、控制、收事件
   终端用户全程看不到任何 Home Assistant 界面。
```

## 安装（Home Assistant OS / Supervised）

1. 把这两个插件目录放进一个 Git 仓库（每个插件一个子目录，内含 `config.yaml`）。
2. HA → 设置 → 加载项 → 右上角"加载项商店" → 右上角 ⋮ → **仓库** → 填入你的仓库地址。
3. 商店里出现 `White-Label API Proxy` 和 `White-Label Pairing` → 安装。
4. 打开 `White-Label Pairing` → 配置：
   - `pairing_code`：印在设备上的配对码
   - `master_token`：管理员主长期令牌（在 HA 个人资料页生成，`password` 类型会隐藏）
   - `token_lifespan_days`：签发令牌有效期（默认 3650 天）
5. 启动两个插件。

> 两个插件都在 HAOS 内部用官方 DNS 名 `homeassistant.local.hass.io:8123` 寻址 HA 内核，
> 无需手动配置 IP。

## 安全要点

- **配对端口 8099 仅限局域网**：插件绑定在网关 LAN 上，不要在路由器上做端口转发。
- **远程访问**另走你自己的云中转（见主 README 第 4 步），不要把 8080/8099 直接暴露公网。
- **主令牌**是高权限凭证，仅存于配对插件配置中，绝不发给用户或 App。
- **每设备独立令牌**：可单独吊销（在 HA 个人资料页删除对应令牌），互不影响。
- 生产环境对 8080 应上 TLS（`wss://`），可在前面再套一层带证书的反代。

## 本地如何验证（无需 HAOS）

配对插件 `pairing.py` 在没有 `SUPERVISOR_TOKEN` 时会回退到环境变量，因此可以用模拟服务器验证：

```bash
# 终端 1：模拟 HA（接受主令牌 demo-token-123，并签发 LLT-* 令牌）
.venv/bin/python ha-whitelabel-demo/mock-server/mock_ha_server.py

# 终端 2：配对插件，指向模拟服务器
HA_HOST=127.0.0.1 HA_PORT=8123 PAIRING_CODE=TEST123 MASTER_TOKEN=demo-token-123 \
LISTEN_PORT=8099 .venv/bin/python \
  ha-whitelabel-demo/addon-repo/whitelabel_pairing/rootfs/app/pairing.py

# 终端 3：配对拿令牌
curl -X POST http://127.0.0.1:8099/pair \
  -H "Content-Type: application/json" \
  -d '{"pairing_code":"TEST123","client_name":"MyApp"}'
# -> {"access_token":"LLT-xxxx...","expires_in_days":3650}

# 终端 3：用该令牌控制设备（复用参考客户端）
HA_TOKEN="LLT-xxxx..." .venv/bin/python ha-whitelabel-demo/reference-client/client.py
# -> auth_ok → get_states → 开灯 success → 收到 state_changed 事件
```

## 关键技术点（均已查证 HA 官方源码/文档）

| 点 | 结论 | 来源 |
|----|------|------|
| 程序化签发长期令牌 | WebSocket 命令 `auth/long_lived_access_token`（参数 `client_name`/`lifespan`，返回令牌） | `home-assistant/core` 的 `components/auth/__init__.py` |
| 插件寻址 HA 内核 | `homeassistant.local.hass.io:8123` | 官方 `nginx_proxy` 插件配置 |
| 插件读自身配置 | `GET http://supervisor/addons/self/info`（带 `X-Supervisor-Token` 头，调用方可读自己的 options） | `developers.home-assistant` Supervisor API 文档 |

## 法律

- HA 核心代码为 Apache 2.0，商用需保留版权/许可证声明。
- 「Home Assistant」名称与 Logo 为 Nabu Casa 商标，勿用作自有品牌或官方背书。
