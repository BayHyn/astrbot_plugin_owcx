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
            backoff = 2 **attempt
            if time.time() + backoff >= deadline:
                logger.warning("[OWAPI] 剩余时间不足，放弃重试")
                break
            await asyncio.sleep(backoff)
        return None

    # 核心接口
    async def get_summary(self, tag: str) -> Optional[Dict[str, Any]]:
        url = f"{OW_API}/players/{tag.replace('#', '-')}/summary"
        return await self._get(url, timeout=8)

    async def get_comp_summary(self, tag: str) -> Optional[Dict[str, Any]]:
        url = f"{OW_API}/players/{tag.replace('#', '-')}/stats/summary?gamemode=competitive"
        return await self._get(url, timeout=10, silent=True)

    async def get_qp_summary(self, tag: str) -> Optional[Dict[str, Any]]:
        url = f"{OW_API}/players/{tag.replace('#', '-')}/stats/summary?gamemode=quickplay"
        return await self._get(url, timeout=10, silent=True)


# ---------- 插件 ----------
DIVISION_CN = {
    "bronze": "青铜", "silver": "白银", "gold": "黄金",
    "platinum": "白金", "diamond": "钻石", "master": "大师",
    "grandmaster": "宗师"
}

# 角色名中英文映射（核心修改1）
ROLE_CN = {
    "tank": "坦克",
    "damage": "输出",
    "support": "支援"
}

def div_to_sr(div: Optional[str], tier: Optional[int]) -> str:
    if not div or tier is None:
        return "未定级"  # 核心修改2：未定级时明确显示
    cn = DIVISION_CN.get(div, div.upper())
    return f"{cn} {tier}"

def fmt_duration(sec: int) -> str:
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m}m"

# 模式数据格式化
def format_mode(general: Dict[str, Any], mode_name: str) -> str:
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

    return (
        f"【{mode_name}】\n"
        f"📊 总场次 {gp}  胜 {gw}  负 {gl}  胜率 {wr:.1f}%  综合KD {kd:.2f}\n"
        f"🎯 平均数据（每10min）\n"
        f"　消灭 {elim_avg:.1f}  死亡 {death_avg:.1f}  "
        f"伤害 {dmg_avg:.0f}  治疗 {heal_avg:.0f}"
    )


@register("astrbot_plugin_owcx", "tzyc", "亚服 OW2 全数据查询", "v1.1.1")
class OWStatsPlugin(Star):
    def __init__(self,** kwargs):
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

    # ---- 命令（核心修改：角色名中文 + 强制显示所有角色）----
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

        try:
            summary_task = self.client.get_summary(tag)
            comp_sum_task = self.client.get_comp_summary(tag)
            qp_sum_task = self.client.get_qp_summary(tag)
            summary, comp_sum, qp_sum = await asyncio.gather(summary_task, comp_sum_task, qp_sum_task)

            if not summary:
                await event.send(event.plain_result("❌ 未找到玩家或资料未公开！"))
                return

            # 段位数据提取
            competitive_data = summary.get("competitive", {})
            logger.info(f"[OW段位调试] competitive原始数据: {json.dumps(competitive_data, ensure_ascii=False)}")
            
            # PC优先，无则用主机端数据
            pc_data = competitive_data.get("pc", {})
            console_data = competitive_data.get("console", {})
            use_data = pc_data if pc_data else console_data
            logger.info(f"[OW段位调试] 最终使用的段位数据: {json.dumps(use_data, ensure_ascii=False)}")

            role_lines, season_lines = [], []
            # 遍历三角色（核心修改3：强制显示所有角色，中文名称）
            for role in ["tank", "damage", "support"]:
                # 处理API返回的null（转为空字典）
                role_info = use_data.get(role) or {}
                logger.info(f"[OW段位调试] {role}角色数据: {json.dumps(role_info, ensure_ascii=False)}")
                
                # 角色名转为中文
                role_cn = ROLE_CN[role]
                
                # 提取段位（division + tier），无论是否有数据都显示角色
                div = role_info.get("division")
                tier = role_info.get("tier")
                # 调用div_to_sr，未定级时会返回"未定级"
                role_lines.append(f"{role_cn}: {div_to_sr(div, tier)}")
                
                # 上赛季段位（仅当有数据时添加）
                if (comp_sum is None or comp_sum.get("general", {}).get("games_played", 0) == 0) and role_info.get("season") and div and tier is not None:
                    season_lines.append(
                        f"{role_cn}: {div_to_sr(div, tier)} (S{role_info['season']})"
                    )
            
            season_hint = f"📌 上赛季段位 | {' | '.join(season_lines)}\n" if season_lines else ""

            # 竞技模式数据
            if comp_sum and comp_sum.get("general", {}).get("games_played", 0):
                comp_block = format_mode(comp_sum["general"], "竞技")
            else:
                comp_block = "【竞技】\n暂无数据"

            # 快速模式数据
            if qp_sum and qp_sum.get("general", {}).get("games_played", 0):
                qp_block = "\n\n" + format_mode(qp_sum["general"], "快速")
            else:
                qp_block = "\n\n【快速】\n暂无数据"

            # 组装消息
            msg = (
                f"【{tag}】亚服 OW2 全数据\n"
                f"🏆 当前段位 | {' | '.join(role_lines)}\n"
                f"{season_hint}"
                f"{comp_block}{qp_block}"
            )
            await event.send(event.plain_result(msg))

        except Exception as e:
            logger.error(f"[OW查询异常] {type(e).__name__}: {e}", exc_info=True)
            await event.send(event.plain_result("❌ 查询异常，请稍后重试"))
            return

    # ---- 其他命令保留 ----
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
            "5. /ow帮助"
        )

    @filter.command("ow状态")
    async def ow_status(self, event: AstrMessageEvent):
        total = len(self.bind)
        ok = await self.client.get_summary("TeKrop-2217") is not None
        yield event.plain_result(
            f"🔧 插件状态\n"
            f"API 连通: {'✅' if ok else '❌'}\n"
            f"已绑定: {total} 人\n"
            f"版本: v1.1.1"
        )

    async def terminate(self):
        logger.info("OW 插件卸载，保存绑定...")
        self._save_bind(self.bind)
        logger.info("OW 插件已卸载")