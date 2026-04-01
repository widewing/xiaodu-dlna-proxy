# xiaodu-dlna-proxy

为小度智能音箱提供一个“稳定身份”的 UPnP 代理。

有些小度音箱会在一段时间后自己更换 UPnP `uuid`。对很多控制点来说，这不只是一个显示字段变化，而是“这是一台新设备”。结果通常是：

- 收藏、自动化或设备绑定失效
- 控制端重复发现出多台“看起来像同一个音箱”的设备
- 某些客户端缓存了旧 `uuid` 之后，需要手工删除再重新发现

这个代理的目标不是重做一套 DLNA/UPnP 协议栈，而是在尽量少改动原设备行为的前提下，给小度包装出一个稳定、可重复发现的身份。

## 它做什么

代理会尽量复用原音箱的能力，只把“身份”这一层稳定下来。具体来说：

- 提供新的 `description.xml`
- 响应和广播 SSDP，让控制点发现到这个固定 `uuid`
- 把根设备 `friendlyName` 改成 `原名 (proxy)`，和原设备区分开
- 把根设备 `UDN` 改成稳定值
- 把其他 `/upnp/...` 请求透明转发到原音箱

默认情况下，这个稳定 `uuid` 会基于代理宿主机的 MAC 地址生成，所以同一台机器重启后仍然保持不变；如果想完全手工控制，也可以显式传入 `--fixed-uuid`。

## 上游发现方式

这个项目现在只支持一种上游定位方式：用 `--upstream-friendly-name` 通过 SSDP 发现设备，再按 `friendlyName` 精确匹配。

这样做是因为小度不仅 `uuid` 会变，连 `description.xml` 的端口也会变。把上游地址写死在启动参数里，本质上还是不稳定。现在代理会先发一次 SSDP `M-SEARCH`，找到当前设备广播出来的 `LOCATION`，再去拉最新的 `description.xml`。后续如果上游端口再次变化，代理也会在刷新或转发失败时重新发现一次。

## 运行

```bash
python3 proxy_upnp.py \
  --upstream-friendly-name '小度智能音箱-2026' \
  --advertise-host '192.168.1.10' \
  --http-port 18080
```

参数说明：

- `--upstream-friendly-name`: 通过 SSDP 发现上游设备时，按这个 `friendlyName` 匹配
- `--fixed-uuid`: 可选。手工指定的固定 `uuid`
- `--advertise-host`: 局域网里其他设备能访问到这台代理机器的 IP
- `--http-port`: 代理的 HTTP 端口，`LOCATION` 会指到这里

如果不传 `--advertise-host`，程序会根据已经解析到的上游设备地址自动探测本机局域网 IP。
如果不传 `--fixed-uuid`，程序会基于本机 MAC 地址生成一个稳定 UUID。

## Docker

仓库里已经带了 [Dockerfile](/Users/guani/Development/xiaodu-dlna-proxy/Dockerfile) 和 [compose.yaml](/Users/guani/Development/xiaodu-dlna-proxy/compose.yaml)。

先修改 `compose.yaml` 里的 `UPSTREAM_FRIENDLY_NAME`，然后启动：

```bash
docker compose up -d --build
```

compose 默认使用 `network_mode: host`，因为 SSDP 发现依赖 `1900/udp` 组播。只想手工构建镜像的话：

```bash
docker build -t xiaodu-dlna-proxy .
docker run --rm --network host \
  -e UPSTREAM_FRIENDLY_NAME='小度智能音箱-2026' \
  xiaodu-dlna-proxy
```

可选环境变量：

- `UPSTREAM_FRIENDLY_NAME`: 通过 SSDP 按设备名发现上游设备
- `ADVERTISE_HOST`: 手工覆盖自动探测的对外 IP
- `FIXED_UUID`: 手工覆盖基于本机 MAC 派生的 UUID
- `HTTP_PORT`: 默认 `18080`
- `LOG_LEVEL`: 默认 `INFO`

## 验证

启动后可以先看：

```bash
curl -s http://127.0.0.1:18080/description.xml
```

确认里面：

- `<UDN>` 已经变成固定 `uuid`
- `<URLBase>` 指向原音箱
- `/upnp/...` 这些相对路径已经被展开成原音箱的绝对地址

如果控制点是通过 SSDP 发现设备，它会拿到：

- 固定 `USN`
- 代理自己的 `LOCATION`
- 代理 description 里指向原音箱的控制地址

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```
