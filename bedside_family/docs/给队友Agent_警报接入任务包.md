# 任务包:跌倒检测端 → 家属App 警报接入(给 AI Agent 的完整简报)

> 你是队友侧的开发 agent。你的任务:在跌倒检测代码"判定疑似倒地"的那一刻,向床头执行端上报一条 HTTP 警报。
> 本包自包含全部契约与实测样例,读完即可开工,无需向人间确认。

## 0. 一句话契约

```
POST http://10.153.47.252:8013/fall_alarm
Content-Type: application/json
{"kind":"fall","src":"cam-A","conf":0.9,"event_id":"<你们生成的唯一事件号>"}
```

- 与床头**同一 WiFi** 才可达(现场限制;4G 跨网等中转站落地后此地址不变)。
- 空 body 也收(兼容);带 event_id 则网络重试**自动去重、只响一次**。

## 1. 端点全集(仅前 4 项与检测端相关)

| 方法 | 路径 | 作用 | 副作用 |
|---|---|---|---|
| POST | `/fall_alarm` | 跌倒上报 | 建 1 条警报 → 家属手机响 |
| POST | `/fall_alarm`(body `{"dry":true}`) | 无扰联调测试 | **无**(不落账、手机不响) |
| GET | `/fall_alarm` | 健康检查/版本核对 | 无 |
| GET | `/alarms` | 警报历史(新在前,50条,字段含 acked) | 无 |
| POST | `/alarms/ack` body `{"id":"al_..."}` | 家属app确认用 | 置 acked=true |

## 2. 请求字段(全部可选,但推荐都带)

| 字段 | 类型 | 说明 |
|---|---|---|
| kind | str | 默认 "fall";≤20字符 |
| src | str | 来源标签,如 "cam-A";≤40字符 |
| conf | float | 置信度 0~1,服务端保留 2 位 |
| event_id | str | **强烈推荐**:同一事件的唯一号,≤64字符;同号重发去重 |
| dry | bool | true 即无扰测试 |

非法 JSON body 也收(按无 body 处理,src 记为 "@raw字节数")——不会 4xx,放心发。

## 3. 回执语义(实测原样,2026-10-24 首发新事件)

```json
{"ok": true, "dedup": false, "id": "al_20261024_221933_612", "time": "2026-10-24 22:19:33", "kind": "fall", "acked": false, "event_id": "teammate-e100", "src": "cam-A", "conf": 0.88}
```

同 event_id 重发(实测原样)——注意 `dedup:true` 且 id 不变,不会重响:

```json
{"ok": true, "dedup": true, "id": "al_20261024_221933_612", "time": "2026-10-24 22:19:33", "kind": "fall", "acked": false, "event_id": "teammate-e100", "src": "cam-A", "conf": 0.88}
```

dry 测试回执(实测原样):

```json
{"ok": true, "dry": true, "ver": "5.7", "echo": {"kind": "fall", "src": "cam-A", "conf": 0.9, "event_id": "drycheck-1"}}
```

- `ok:true` = **床头已收到并落账**;不代表手机已收到,也没有回执链路回传检测端。fire-and-forget 即可。
- 手机侧为拉模式:家属 app 前台 3 秒内拉到并全屏响铃+震动;app 关着由常驻守护服务 10 秒内弹系统级高优通知。家属点「知道了」→ 该条 acked。
- 连不上 = TCP 超时/连接拒绝 → 走带退避的重试,**重试必须复用同一 event_id**。
- id 格式 `al_日期_时分秒_毫秒`,同秒并发不撞号(毫秒化保证)。

## 4. 直接可抄的接入代码(Python 3 标准库)

```python
import json, time, uuid, urllib.request

FAMILY_ALARM = "http://10.153.47.252:8013/fall_alarm"

def report_fall(conf=None, src="cam-A"):
    """判定倒地时调用一次。返回服务端回执 dict;连不上返回 {"ok": False}。"""
    eid = uuid.uuid4().hex[:12]                    # 每个倒地事件一个号,重试复用
    body = {"kind": "fall", "src": src, "event_id": eid}
    if conf is not None:
        body["conf"] = round(float(conf), 2)
    for attempt in range(3):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                FAMILY_ALARM, data=json.dumps(body).encode(),
                method="POST", headers={"Content-Type": "application/json"}), timeout=3)
            return json.loads(r.read().decode())
        except Exception:
            time.sleep(1 + attempt)                # 退避;event_id 不变 → 去重保平安
    return {"ok": False}
```

自检两连(不响手机):

```python
print(urllib.request.urlopen("http://10.153.47.252:8013/fall_alarm", timeout=3).read().decode())
# → {"ok": true, "service": "arm_service", "ver": "5.7", ...}
```

## 5. 验收清单(agent 自测用,全部无扰或事后 ack)

1. `GET /fall_alarm` 返回 `"ver":"5.7"` → 链路通、版本对。
2. `POST {"dry":true}` → 返回 `"dry":true`;随后 `GET /alarms` 条数不变 → 字段格式校验过。
3. 演示彩排:真实 POST 一次(会响家属手机)→ 手机 3 秒内红条+铃+震;app 杀掉再发一条 → 10 秒内弹系统通知。
4. 加分项:同一 event_id 连发两次 → `GET /alarms` 只多 1 条。
5. **纪律:联调试格式一律用 dry;真警报只用于彩排。**

## 6. 版本与代码出处

- 床头服务:arm_service **v5.7**(systemd `arm_service`,掉电自动恢复,警报存 `/root/caremate/alarms/alarms.json`)。
- 代码:团队仓库 `Elderly-helper/bedside_family/service_handboard/arm_service.py`(GitHub 推送中);板上副本 `/root/caremate/hand_controller/arm_service.py`。
- v5.6→v5.7 变更:只加不破——新增 event_id 去重、dry 模式、 richer 健康检查、id 毫秒化;空 body 老姿势行为不变。

—— Mocen CareMate 家属端 · 2026-10-24