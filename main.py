# main.py  –  AstrBot 亚服 OW2 战绩查询
from astrbot.api.star import Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import aiohttp
import asyncio
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
import time

# ---------- 工具 ----------
class TimedCache:
    def __init__(self, ttl: int = 600):
        self.ttl = ttl
        self._data: Dict[str, tuple] = {}

    def get(self, key: str) -> Optional[Any]:
        if key not in self._data:
            return None
        expire, value = self._data[key]
        if time.time() > expire:
            self._data.pop(key)
            return None
        return value

    def set(self, key: str, value: Any):
        self._data[key] = (time.time() + self.ttl, value)


# ---------- 全局限流器 ----------
class RateLimiter:
    """令牌桶 + 429 冻结"""
    def __init__(self, rate: float = 1.0, burst: int = 3):
        self._rate = rate
        self._burst = burst
        self._tokens = burst
        self._last = time.time()
        self._freeze_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, timeout: float = 35) -> bool:
        async with self._lock:
            while True:
                now = time.time()
                if now >= self._freeze_until:
                    added = (now - self._last) * self._rate
                    self._tokens = min(self._burst, self._tokens + added)
                    self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return True
                sleep_for = min(1 / self._rate, self._freeze_until - now)
                if sleep_for <= 0:
                    continue
                if timeout <= 0:
                    return False
                await asyncio.sleep(min(sleep_for, timeout))
                timeout -= sleep_for

    def freeze(self, seconds: int):
        self._freeze_until = max(self._freeze_until, time.time() + seconds)


# ---------- API ----------
OW_API = "https://overfast-api.tekrop.fr"


class OWAPIClient:
    limiter = RateLimiter(rate=1.0, burst=3)

    def __init__(self, timeout: int = 35, max_retries: int = 3):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.cache = TimedCache(ttl=600)

    async def _get(self, url: str, timeout: int = 35, silent: bool = False) -> Optional[Dict[str, Any]]:
        if self.cache.get(url):
            return self.cache.get(url)
        deadline = time.time() + timeout
        for attempt in range(1, self.max_retries + 1):
            ok = await self.limiter.acquire(timeout=deadline - time.time())
            if not ok:
                logger.warning("[OWAPI] 全局等待超时，放弃请求")
                return None
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.get(url) as resp:
                        if not silent:
                            logger.info(f"[OWAPI] {url} -> {resp.status}")
                        if resp.status == 200:
                            data = await resp.json()
                            self.cache.set(url, data)
                            return data
                        if resp.status == 404:
                            logger.debug(f"[OWAPI] 404 无数据: {url}")
                            return None
                        if resp.status == 429:
                            retry_after = int(resp.headers.get("Retry-After", 5))
                            logger.warning(f"[OWAPI] 429 限流，冻结 {retry_after}s")
                            self.limiter.freeze(retry_after)
                            continue
                        logger.warning(f"[OWAPI] 非 200/404/429: {resp.status}")
            except asyncio.TimeoutError:
                logger.warning(f"[OWAPI] 请求超时（尝试{attempt}）| url={url}")
            except Exception as e:
                logger.error(f"[OWAPI] 请求异常（尝试{attempt}）: {type(e).__name__}: {e} | url={url}")
            backoff = 2 ** attempt
            if time.time() + backoff >= deadline:
                logger.warning("[OWAPI] 剩余时间不足，放弃重试")
                break
            await asyncio.sleep(backoff)
        return None

    # 五个原子接口
    async def get_summary(self, tag: str) -> Optional[Dict[str, Any]]:
        url = f"{OW_API}/players/{tag.replace('#', '-')}/summary"
        return await self._get(url, timeout=8)

    async def get_comp_summary(self, tag: str) -> Optional[Dict[str, Any]]:
        url = f"{OW_API}/players/{tag.replace('#', '-')}/stats/summary?gamemode=competitive"
        return await self._get(url, timeout=10, silent=True)

    async def get_comp_heroes(self, tag: str) -> Optional[Dict[str, Any]]:
        url = f"{OW_API}/players/{tag.replace('#', '-')}/stats/heroes?gamemode=competitive"
        return await self._get(url, timeout=10, silent=True)

    async def get_qp_summary(self, tag: str) -> Optional[Dict[str, Any]]:
        url = f"{OW_API}/players/{tag.replace('#', '-')}/stats/summary?gamemode=quickplay"
        return await self._get(url, timeout=10, silent=True)

    async def get_qp_heroes(self, tag: str) -> Optional[Dict[str, Any]]:
        url = f"{OW_API}/players/{tag.replace('#', '-')}/stats/heroes?gamemode=quickplay"
        return await self._get(url, timeout=10, silent=True)


# ---------- 插件 ----------
DIVISION_CN = {
    "bronze": "青铜", "silver": "白银", "gold": "黄金",
    "platinum": "白金", "diamond": "钻石", "master": "大师",
    "grandmaster": "宗师"
}

def div_to_sr(div: Optional[str], tier: Optional[int]) -> str:
    if not div or tier is None:
        return "未定位"
    cn = DIVISION_CN.get(div, div.upper())
    return f"{cn} {tier}"

def pick_top_heroes(heroes_list: List[Dict[str, Any]], n: int = 5) -> List[Dict[str, Any]]:
    heroes_list.sort(key=lambda x: x.get("time_played", 0), reverse=True)
    return heroes_list[:n]

def fmt_duration(sec: int) -> str:
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m}m"

def format_mode(general: Dict[str, Any], heroes: List[Dict[str, Any]], mode_name: str) -> str:
    gp = general.get("games_played", 0)
    gw = general.get("games_won", 0)
    gl = gp - gw
    wr = (gw / gp * 100) if gp else 0.0
    kd = general.get("kda", 0)
    avg = general.get("average", {})
    elim_avg = avg.get("eliminations", 0)
    death_avg = avg.get("deaths", 0)
    dmg_avg = avg.get("damage", 0)
    heal_avg = avg.get("healing", 0)

    top = pick_top_heroes(heroes)
    hero_lines = []
    for h in top:
        name = h["key"].capitalize()
        wr_h = h.get("winrate", 0)
        kt = h.get("time_played", 0)
        kda_h = h.get("kda", 0)
        hero_lines.append(
            f"{name}  胜率{wr_h:.1f}%  时长{fmt_duration(kt)}  KD{kda_h:.2f}"
        )

    return (
        f"【{mode_name}】\n"
        f"📊 总场次 {gp}  胜 {gw}  负 {gl}  胜率 {wr:.1f}%  综合KD {kd:.2f}\n"
        f"🎯 平均数据（每10min）\n"
        f"　消灭 {elim_avg:.1f}  死亡 {death_avg:.1f}  "
        f"伤害 {dmg_avg:.0f}  治疗 {heal_avg:.0f}\n"
        f"🎮 常玩英雄 TOP5\n" + ("\n".join(hero_lines) if hero_lines else "　暂无数据")
    )


@register("astrbot_plugin_owcx", "tzyc", "亚服 OW2 全数据查询", "v1.1.0")
class OWStatsPlugin(Star):
    def __init__(self, **kwargs):
        super().__init__(kwargs.get("context"))
        self.client = OWAPIClient()
        self.bind_file = Path("data/ow_stats_bind.json")
        self.bind_file.parent.mkdir(parents=True, exist_ok=True)
        self.bind = self._load_bind()

    # ---- 绑定 ----
    def _load_bind(self) -> Dict[str, str]:
        if self.bind_file.exists():
            try:
                return json.loads(self.bind_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"加载绑定失败: {e}")
        return {}

    def _save_bind(self, data: Dict[str, str]):
        try:
            self.bind_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"保存绑定失败: {e}")

    # ---- 命令 ----
    @filter.command("ow")
    async def ow_stats(self, event: AstrMessageEvent):
        arg = event.message_str.strip().removeprefix("ow").strip()
        qq = str(event.get_sender_id())
        tag = arg or self.bind.get(qq)
        if not tag:
            yield event.plain_result("请先绑定：/ow绑定 玩家#12345\n或直接查询：/ow 玩家#12345")
            return
        if "#" not in tag:
            yield event.plain_result("格式错误！示例：玩家#12345")
            return

        yield event.plain_result(f"正在查询 {tag} ...")

        async def gather_all():
            return await asyncio.gather(
                self.client.get_summary(tag),
                self.client.get_comp_summary(tag),
                self.client.get_comp_heroes(tag),
                self.client.get_qp_summary(tag),
                self.client.get_qp_heroes(tag)
            )

        summary, comp_sum, comp_hero, qp_sum, qp_hero = await gather_all()
        if not summary:
            await event.send(event.plain_result("❌ 未找到玩家或资料未公开！"))
            return

        pc = (summary.get("competitive") or {}).get("pc") or {}
        role_lines, season_lines = [], []
        for r in ["tank", "damage", "support"]:
            info = pc.get(r)
            if info:
                role_lines.append(f"{r.capitalize()}: {div_to_sr(info.get('division'), info.get('tier'))}")
                # 本赛季无场次 → 显示上赛季
                if (comp_sum is None or comp_sum.get("general", {}).get("games_played", 0) == 0) and info.get("season"):
                    season_lines.append(
                        f"{r.capitalize()}: {div_to_sr(info.get('division'), info.get('tier'))} (S{info['season']})"
                    )
        season_hint = f"📌 上赛季段位 | {' | '.join(season_lines)}\n" if season_lines else ""

        # 竞技
        if comp_sum and comp_sum.get("general", {}).get("games_played", 0):
            comp_block = format_mode(comp_sum["general"], comp_hero.get("heroes", []) if comp_hero else [], "竞技")
        else:
            comp_block = "【竞技】\n暂无数据"

        # 快速
        if qp_sum and qp_sum.get("general", {}).get("games_played", 0):
            qp_block = "\n\n" + format_mode(qp_sum["general"], qp_hero.get("heroes", []) if qp_hero else [], "快速")
        else:
            qp_block = "\n\n【快速】\n暂无数据"

        msg = (
            f"【{tag}】亚服 OW2 全数据\n"
            f"🏆 当前段位 | {' | '.join(role_lines)}\n"
            f"{season_hint}"
            f"{comp_block}{qp_block}"
        )
        await event.send(event.plain_result(msg))

    @filter.command("ow绑定")
    async def ow_bind(self, event: AstrMessageEvent):
        arg = event.message_str.strip().removeprefix("ow绑定").strip()
        qq = str(event.get_sender_id())
        if not arg or "#" not in arg:
            yield event.plain_result("格式：/ow绑定 玩家#12345")
            return
        self.bind[qq] = arg
        self._save_bind(self.bind)
        yield event.plain_result(f"✅ 已绑定 {arg}\n现在可直接用 /ow 查询")

    @filter.command("ow解绑")
    async def ow_unbind(self, event: AstrMessageEvent):
        qq = str(event.get_sender_id())
        old = self.bind.pop(qq, None)
        if old:
            self._save_bind(self.bind)
            yield event.plain_result(f"✅ 已解绑 {old}")
        else:
            yield event.plain_result("您还未绑定")

    @filter.command("ow帮助")
    async def ow_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🎮 亚服 OW2 全数据命令\n"
            "1. /ow 玩家#12345  – 直接查别人\n"
            "2. /ow绑定 玩家#12345 – 绑定自己\n"
            "3. /ow – 查已绑定账号\n"
            "4. /ow解绑\n"
            "5. /ow帮助\n\n"
            "竞技数据立即返回，快速数据后台补发~"
        )

    @filter.command("ow状态")
    async def ow_status(self, event: AstrMessageEvent):
        total = len(self.bind)
        ok = await self.client.get_summary("TeKrop-2217") is not None
        yield event.plain_result(
            f"🔧 插件状态\n"
            f"API 连通: {'✅' if ok else '❌'}\n"
            f"已绑定: {total} 人\n"
            f"版本: v1.1.0"
        )

    async def terminate(self):
        logger.info("OW 插件卸载，保存绑定...")
        self._save_bind(self.bind)
        logger.info("OW 插件已卸载")