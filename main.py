from astrbot.api.star import Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import aiohttp
import json
from pathlib import Path

API_BASE = "https://over-api.itzdrli.com/v1"   # 国内镜像
BIND_FILE = Path("data/ow_stats_bind.json")
BIND_FILE.parent.mkdir(exist_ok=True)

# -------------- 工具函数 --------------
def _load_bind() -> dict:
    return json.loads(BIND_FILE.read_text()) if BIND_FILE.exists() else {}

def _save_bind(data: dict):
    BIND_FILE.write_text(json.dumps(data, ensure_ascii=False))

# -------------- 插件主体 --------------
@register("ow_stats", "YourName", "亚服 OW2 战绩查询", "v1.0.0")
class OWStatsPlugin(Star):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.bind = _load_bind()

    # 指令：.ow  或  .ow 玩家#12345
    @filter.command("ow")
    async def ow_stats(self, event: AstrMessageEvent):
        arg = event.message_str.removeprefix("/ow").strip()
        qq = event.sender.user_id
        tag = arg or self.bind.get(qq)
        if not tag:
            yield event.plain_result("请先绑定战网 Tag：/ow绑定 玩家#12345")
            return

        url = f"{API_BASE}/players/{tag.replace('#', '-')}/summary"
        try:
            async with aiohttp.ClientSession() as ses:
                async with ses.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        yield event.plain_result("查询失败，请检查 Tag 是否正确或资料是否公开。")
                        return
                    data = await resp.json()
        except Exception as e:
            logger.exception(e)
            yield event.plain_result("接口请求异常，稍后再试。")
            return

        tank = data.get("tank", {}).get("peak", 0)
        dps  = data.get("damage", {}).get("peak", 0)
        sup  = data.get("support", {}).get("peak", 0)
        msg = f"【{tag}】亚服 OW2 最高 SR\n坦克：{tank} | 输出：{dps} | 辅助：{sup}"
        yield event.plain_result(msg)

    # 指令：/ow绑定 玩家#12345
    @filter.command("ow绑定")
    async def ow_bind(self, event: AstrMessageEvent):
        arg = event.message_str.removeprefix("/ow绑定").strip()
        if not arg or "#" not in arg:
            yield event.plain_result("格式：/ow绑定 玩家#12345")
            return
        self.bind[event.sender.user_id] = arg
        _save_bind(self.bind)
        yield event.plain_result("绑定成功！下次可直接 /ow 查询。")