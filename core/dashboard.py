from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import os
import platform
import sys
import time
from typing import Any, Optional
import zlib

import aiohttp
from tqdm import tqdm

from core import stats, system, utils, web
from core import cluster
from core.api import StatsCache
from core import scheduler

from core.const import *


@dataclass
class Token:
    value: str
    create_at: float


@dataclass
class StorageInfo:
    name: str
    type: str
    endpoint: str
    size: int
    free: int


@dataclass
class ProgressBar:
    object: Optional[tqdm] = None
    desc: str = ""
    last_value: float = 0
    speed: float = 0
    show: Optional[int] = None


websockets: list["web.WebSocket"] = []
last_status = ""
last_text = ""
cur_tqdm: ProgressBar = ProgressBar()
task_tqdm: Optional[int] = None
tokens: list[Token] = []
authentication_module: list[str] = [
    'storages',
    'version',
]


def deserialize(data: utils.DataInputStream):
    match (data.readVarInt()):
        case 0:
            return data.readString()
        case 1:
            return data.readBoolean()
        case 2:
            return int(data.readString())
        case 3:
            return float(data.readString())
        case 4:
            return [deserialize(data) for _ in range(data.readVarInt())]
        case 5:
            return {
                deserialize(data): deserialize(data) for _ in range(data.readVarInt())
            }
        case 6:
            return None


def serialize(data: Any):
    buf = utils.DataOutputStream()
    if isinstance(data, str):
        buf.writeVarInt(0)
        buf.writeString(data)
    elif isinstance(data, bool):
        buf.writeVarInt(1)
        buf.writeBoolean(data)
    elif isinstance(data, float):
        buf.writeVarInt(2)
        buf.writeString(str(data))
    elif isinstance(data, int):
        buf.writeVarInt(3)
        buf.writeString(str(data))
    elif isinstance(data, list):
        buf.writeVarInt(4)
        buf.writeVarInt(len(data))
        buf.write(b"".join((serialize(v).io.getvalue() for v in data)))
    elif isinstance(data, dict):
        buf.writeVarInt(5)
        buf.writeVarInt(len(data.keys()))
        buf.write(
            b"".join(
                (
                    serialize(k).io.getvalue() + serialize(v).io.getvalue()
                    for k, v in data.items()
                )
            )
        )
    elif is_dataclass(data):
        buf.write(serialize(asdict(data)).io.getvalue())
    elif data is None:
        buf.writeVarInt(6)
    return buf


async def process(type: str, data: Any):
    if type == "uptime":
        return float(os.getenv("STARTUP") or 0)
    if type == "dashboard":
        return {"hourly": stats.hourly(), "days": stats.daily()}
    if type == "qps":
        c = web.statistics.get_time()
        c -= c % 5
        raw_data = {
            k: v for k, v in web.statistics.get_all_qps().items() if k > c - 300
        }
        resp_data: dict = {}
        for _ in range(c - 300, c, 5):
            resp_data[_] = 0
            for __ in range(5):
                resp_data[_] += raw_data.get(__ + _, 0)
        return {utils.format_time(k): v for k, v in resp_data.items()}
    if type == "status":
        resp: dict = {
            "key": last_status,
        }
        if cur_tqdm is not None and cur_tqdm.object is not None:
            resp.update(
                {
                    "progress": {
                        "value": cur_tqdm.object.n,
                        "total": cur_tqdm.object.total,
                        "speed": cur_tqdm.speed,
                        "unit": cur_tqdm.object.unit,
                        "desc": cur_tqdm.desc,
                    }
                }
            )
        return resp
    if type == "master":
        async with aiohttp.ClientSession(BASE_URL) as session:
            async with session.get(data) as resp:
                return resp.json()
    if type == "system":
        return {
            "memory": system.get_used_memory(),
            "connections": system.get_connections(),
            "cpu": system.get_cpus(),
            "cache": (
                asdict(await get_cache_stats()) if cluster.cluster else StatsCache()
            ),
        }
    if type == "version":
        return {"cur": cluster.VERSION, "latest": cluster.fetched_version, "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", "os": platform.platform()}
    if type == "global_stats":
        day = 1
        if isinstance(data, dict):
            t = data.get("type", 0)
            if t == 1:
                day = 7
            elif t == 2:
                day == 30
        return stats.daily_global(day)
    if type == "system_details":
        return system.get_loads_detail()


async def get_cache_stats() -> StatsCache:
    stat = StatsCache()
    for storage in cluster.storages.get_storages():
        t = await storage.get_cache_stats()
        stat.total += t.total
        stat.bytes += t.bytes
    return stat


async def set_status_by_tqdm(text: str, pbar: tqdm):
    global cur_tqdm, task_tqdm
    cur_tqdm.object = pbar
    cur_tqdm.desc = text
    if task_tqdm:
        scheduler.cancel(task_tqdm)
        scheduler.cancel(cur_tqdm.show)
    cur_tqdm.speed = 0
    cur_tqdm.last_value = 0
    task_tqdm = scheduler.repeat(_calc_tqdm_speed, delay=0, interval=0.5)
    cur_tqdm.show = scheduler.repeat(_set_status, kwargs={
        "blocked": True
    }, delay=0, interval=1)


async def _calc_tqdm_speed():
    global cur_tqdm
    if cur_tqdm.object is None or cur_tqdm.object.disable:
        if task_tqdm is not None:
            scheduler.cancel(task_tqdm)
        scheduler.cancel(cur_tqdm.show)
        cur_tqdm.object = None
        await _set_status(blocked=True)
        return
    cur_tqdm.speed = (cur_tqdm.object.n - cur_tqdm.last_value) / 0.5
    cur_tqdm.last_value = cur_tqdm.object.n


async def _set_status(text: Optional[str] = None, blocked: bool = False):
    global last_text, last_status
    if not text:
        text = last_text
    if last_status == text and not blocked:
        return
    last_status = text
    await trigger("status")


async def set_status(text):
    global last_text
    if last_text != text:
        await _set_status(text)
    last_text = text


async def trigger(type: str, data: Any = None):
    output = to_bytes(0, type, await process(type, data))
    for ws in websockets:
        await ws.send(output.io.getvalue())


def to_bytes(key: int, type: str, data: Any):
    output = utils.DataOutputStream()
    output.writeVarInt(key)
    output.writeString(type)
    output.write(serialize(data).io.getvalue())
    return output


def generate_token(request: "web.Request") -> Token:
    global tokens
    token = Token(
        hashlib.sha256(
            zlib.compress(
                hashlib.sha512(
                    (
                        request.get_ip()
                        + request.get_user_agent()
                        + request.get_url()
                        + CLUSTER_ID
                        + CLUSTER_SECERT
                        + str(time.time())
                    ).encode("utf-8")
                ).digest()
            )
        ).hexdigest(),
        time.time(),
    )
    tokens.append(token)
    return token


def token_isvaild(value) -> bool:
    for token in tokens:
        if token.value == value and token.create_at + 86400 > time.time():
            return True
    return False
