# Gateway HTTP Proxy

将收到的 HTTP 请求完整转发到 `~/CodeX/Dev/Safactory/gateway` 服务的实际 IP 和端口，再将响应原样返回。使用 Python 标准库，Python 3.10 及以上即可运行，无需安装依赖。

## 启动

gateway 需先启动。例如在 Safactory 根目录执行：

```bash
python -m gateway --config gateway/config.local.yaml
```

在本项目目录通过环境变量指定 **gateway 的 IP 和 HTTP 端口**，然后启动代理：

```bash
GATEWAY_IP=127.0.0.1 GATEWAY_PORT=8000 python3 proxy.py
```

默认监听 `0.0.0.0:9000`。自定义代理监听地址和端口：

```bash
GATEWAY_IP=192.168.1.10 GATEWAY_PORT=8000 \
  PROXY_LISTEN_HOST=127.0.0.1 PROXY_LISTEN_PORT=9001 python3 proxy.py
```

gateway 示例配置的端口是 `8000`，代码默认值是 `8080`，请以正在使用的 gateway 配置中的 `listen_port` 为准。代理与 gateway 在同一机器时，请使用不同的监听端口。

| 环境变量 | 必填 | 默认值 | 用途 |
| --- | --- | --- | --- |
| `GATEWAY_IP` | 是 | 无 | gateway 的 IPv4 或 IPv6 地址 |
| `GATEWAY_PORT` | 是 | 无 | gateway 的 HTTP 端口，范围 1–65535 |
| `PROXY_LISTEN_HOST` | 否 | `0.0.0.0` | 代理监听 IP |
| `PROXY_LISTEN_PORT` | 否 | `9000` | 代理监听端口 |

`GATEWAY_IP`、`GATEWAY_PORT` 缺失、为空或格式无效时，程序会报告错误并退出。gateway 地址不再通过位置参数传入。`--listen-host`、`--listen-port` 仍可使用，并优先于对应环境变量；`--connect-timeout` 和 `--verbose` 保持可用。

## Docker 部署

在本项目目录构建镜像：

```bash
docker build -t gateway-proxy:latest .
```

将 gateway 的地址替换为容器能够访问的实际 IP 和端口，再启动容器：

```bash
docker run -d --name gateway-proxy \
  --restart unless-stopped \
  -p 9000:9000 \
  -e GATEWAY_IP=192.168.1.10 \
  -e GATEWAY_PORT=8000 \
  gateway-proxy:latest
```

容器内的 `127.0.0.1` 指向容器自身。gateway 运行在宿主机时，`GATEWAY_IP` 应使用容器可达的宿主机 IP，gateway 也需监听对应网卡地址。如果在 Linux 上使用 `--network host`，可用 `GATEWAY_IP=127.0.0.1` 连接宿主机 gateway，此时省略 `-p`。

默认容器端口为 `9000`。如设置 `PROXY_LISTEN_PORT`，请同步调整 `-p` 的容器端口。镜像以非 root 用户运行。

```bash
docker logs -f gateway-proxy
docker stop gateway-proxy
```

`docker stop` 会通过 `SIGTERM` 停止代理并关闭活动连接。镜像只包含 Python 运行时和代理程序，无需将 Safactory 源码打包进去。

## 请求示例

调用代理与调用 gateway 的路径、方法和请求体完全相同，只需改访问地址：

```bash
curl http://127.0.0.1:9000/healthz

curl http://127.0.0.1:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"YOUR_ROUTE_KEY","messages":[{"role":"user","content":"hello"}]}'

curl -N http://127.0.0.1:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"YOUR_ROUTE_KEY","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

`YOUR_ROUTE_KEY` 使用 gateway 配置中的模型 route key。也可直接把 SDK 的 `base_url` 改为 `http://127.0.0.1:9000/v1`，或 session 对应的 URL。

## 转发行为

代理为每个客户端 TCP 连接建立一个到 gateway 的连接，并实时双向复制字节流。因此所有 HTTP 方法、原始编码的路径和查询参数、重复 header、Host、Authorization、Cookie、二进制请求体、响应状态、压缩内容、chunked 编码和 trailer 都会保留。支持 HTTP keep-alive、并发连接和 SSE 流式响应，不解析 JSON、不重写路由、不添加鉴权、不自动重试或跟随重定向。

每次读写最多 64 KiB，并使用背压控制缓冲，不等待完整请求或响应。默认只对建立 gateway 连接设置 10 秒超时，可通过 `--connect-timeout` 调整；连接后的请求和响应没有额外超时。gateway 无法连接或连接异常时，代理记录连接错误并关闭连接，不生成额外的 HTTP 响应。日志不记录请求体或 header。

按 `Ctrl+C` 或发送 `SIGTERM` 可停止监听并关闭活动连接。`--verbose` 可开启连接调试日志。

## 已检索的 gateway 接口

根据 `~/CodeX/Dev/Safactory/gateway/app.py` 的接口注册，默认 session 前缀为 `/v1/sessions`：

| 方法 | 路径 |
| --- | --- |
| GET | `/healthz`、`/readyz`、`/metrics` |
| POST | `/v1/chat/completions`、`/v1/responses` |
| POST | `/v1/sessions/{session_id}/chat/completions`、`/v1/sessions/{session_id}/responses` |
| POST | `/v1/sessions/{session_id}/v1/messages`（Anthropic） |
| GET | `/v1/sessions/{session_id}` |
| GET | `/v1/sessions/{session_id}/latest-success-step?model=...` |
| POST | `/v1/sessions/{session_id}/close`、`/v1/sessions/{session_id}/clean` |
| POST | `/v1/sessions/cache/cleanup` |
| GET | `/internal/jobs/{job_id}/environment-row-count` |

gateway 的 session 前缀可配置，以上列表仅用于说明；代理可转发任意路径，包括未列出的接口和 gateway 自身的错误响应。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试验证环境变量配置，并使用本地模拟 gateway 验证原始字节保留、流式转发、长连接、半关闭以及连接清理。
