from astrbot.api.star import Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import PermissionType
from astrbot.api import logger
import aiohttp
import asyncio
import json
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import time
from astrbot.api.message_components import Plain

# ---------- 常量定义 ----------
OW_API = "https://overfast-api.tekrop.fr"
# 段位分数范围映射
DIVISION_SCORE = {
    "bronze": "1-1499", "silver": "1500-1999", "gold": "2000-2499",
    "platinum": "2500-2999", "diamond": "3000-3499", "master": "3500-3999",
    "grandmaster": "4000+"
}
# 角色名中英文映射
ROLE_CN = {"tank": "坦克", "damage": "输出", "support": "支援"}
# 段位中英文映射
DIVISION_CN = {
    "bronze": "青铜", "silver": "白银", "gold": "黄金",
    "platinum": "白金", "diamond": "钻石", "master": "大师",
    "grandmaster": "宗师"
}
# 缓存TTL配置（秒）
CACHE_TTL = {
    "summary": 600,    # 玩家概要：10分钟
    "comp_summary": 600,# 竞技统计：10分钟
    "qp_summary": 600, # 快速（休闲）统计：10分钟
    "hero_stats": 3600 # 英雄数据：1小时
}
# 英雄名-Key映射（扩展可支持更多英雄）
HERO_NAME_TO_KEY = {
    "源氏": "genji","麦克雷": "cassidy","士兵76": "soldier-76",
    "法老之鹰": "pharah","死神": "reaper","猎空": "tracer","温斯顿": "winston",
    "查莉娅": "zarya","莱因哈特": "reinhardt","安娜": "ana","天使": "mercy",
    "卢西奥": "lucio","半藏": "hanzo","狂鼠": "junkrat","路霸": "roadhog",
    "D.Va": "dva","奥丽莎": "orisa","西格玛": "sigma","布里吉塔": "brigitte",
    "莫伊拉": "moira","巴蒂斯特": "baptiste","黑影": "sombra",
    "托比昂": "torbjorn","堡垒": "bastion","美": "mei","艾什": "ashe",
    "破坏球": "wrecking-ball","禅雅塔": "zenyatta","回声": "echo",
    "渣客女王": "junker-queen","雾子": "kiriko","拉玛刹": "ramattra",
    "生命之梭": "lifeweaver","伊拉锐": "illari","毛加": "mauga",
    "探奇": "venture","黑百合": "widowmaker","末日铁拳": "doomfist",
    "秩序之光": "symmetra","索杰恩": "sojourn","骇灾": "hazard","无漾": "wuyang",
    "弗蕾娅": "freya","朱诺": "juno"
}
# 模式映射（默认休闲）
MODE_CN_TO_EN = {"竞技": "competitive", "休闲": "quickplay"}
MODE_EN_TO_CN = {"competitive": "竞技", "quickplay": "休闲"}
DEFAULT_MODE = "quickplay"  # 默认模式：休闲
DEFAULT_MODE_CN = "休闲"    # 默认模式中文显示

# ---------- 工具类 ----------
class TimedCache:
    """带TTL的缓存类"""
    def __init__(self):
        self._data: Dict[str, tuple] = {}

    def get(self, key: str) -> Optional[Any]:
        """获取缓存，过期自动删除"""
        if key not in self._data:
            return None
        expire, value = self._data[key]
        if time.time() > expire:
            self._data.pop(key)
            return None
        return value

    def set(self, key: str, value: Any, ttl: int):
        """设置缓存"""
        self._data[key] = (time.time() + ttl, value)

    def clear(self, pattern: Optional[str] = None):
        """清理缓存，支持模糊匹配"""
        if not pattern:
            self._data.clear()
            return
        keys_to_remove = [k for k in self._data.keys() if pattern in k]
        for key in keys_to_remove:
            self._data.pop(key, None)

    def size(self) -> int:
        """获取缓存大小"""
        return len(self._data)

class RateLimiter:
    """令牌桶限流 + 429冻结机制"""
    def __init__(self, rate: float = 1.0, burst: int = 3):
        self._rate = rate
        self._burst = burst
        self._tokens = burst
        self._last = time.time()
        self._freeze_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, timeout: float = 35) -> bool:
        """获取令牌，超时返回False"""
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
        """冻结指定秒数"""
        self._freeze_until = max(self._freeze_until, time.time() + seconds)

# ---------- API客户端（修复resp异常+超时优化） ----------
class OWAPIClient:
    """守望先锋API客户端（默认休闲模式）"""
    def __init__(self, timeout: int = 60, max_retries: int = 3):  # 超时延长到60秒
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.limiter = RateLimiter(rate=1.0, burst=3)
        self.cache = TimedCache()

    async def _get(self, url: str, ttl: int, timeout: int = 60) -> Tuple[Optional[Dict[str, Any]], str]:
        """基础请求方法，修复resp未赋值+超时优化"""
        cached_data = self.cache.get(url)
        resp = None  # 提前初始化resp，避免未赋值引用
        deadline = time.time() + timeout
        max_attempts = self.max_retries + 1  # 500错误多1次重试

        for attempt in range(1, max_attempts + 1):
            # 获取限流令牌
            ok = await self.limiter.acquire(timeout=deadline - time.time())
            if not ok:
                if cached_data:
                    logger.warning(f"[OWAPI] 请求超时，返回缓存数据: {url}")
                    return cached_data, ""
                return None, "请求超时，当前查询人数过多或服务器响应慢"

            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.get(url) as resp:  # resp仅在此处赋值
                        logger.info(f"[OWAPI] 请求: {url} | 状态码: {resp.status}")

                        # 成功响应
                        if resp.status == 200:
                            data = await resp.json()
                            self.cache.set(url, data, ttl)
                            return data, ""
                        # 404无数据
                        elif resp.status == 404:
                            return None, "未找到该玩家或玩家资料未公开"
                        # 429限流
                        elif resp.status == 429:
                            retry_after = int(resp.headers.get("Retry-After", 5))
                            self.limiter.freeze(retry_after)
                            return None, f"查询过于频繁，请{retry_after}秒后再试"
                        # 500错误处理
                        elif resp.status == 500:
                            logger.error(f"[OWAPI] 服务器内部错误（500）: {url} | 尝试{attempt}/{max_attempts}")
                            if attempt == max_attempts:
                                if cached_data:
                                    logger.warning(f"[OWAPI] 500错误，返回缓存数据: {url}")
                                    return cached_data, ""
                                return None, "服务器暂时无法处理请求（可能是数据同步故障），建议1分钟后重试"
                            await asyncio.sleep(3)
                            continue
                        # 其他错误
                        else:
                            return None, f"服务器请求异常（状态码: {resp.status}），请稍后重试"

            except asyncio.TimeoutError:
                logger.warning(f"[OWAPI] 超时（尝试{attempt}/{max_attempts}）: {url}")
                # 超时后直接重试，不访问resp（此时resp为None）
                backoff = 2 ** attempt
                if time.time() + backoff >= deadline:
                    break
                await asyncio.sleep(backoff)
                continue
            except Exception as e:
                logger.error(f"[OWAPI] 异常（尝试{attempt}/{max_attempts}）: {str(e)} | url={url}")
                # 其他异常也不访问resp，直接重试
                backoff = 2 ** attempt
                if time.time() + backoff >= deadline:
                    break
                await asyncio.sleep(backoff)
                continue

            # 仅当resp存在且非500错误时，执行普通退避（避免resp为None的情况）
            if resp and resp.status != 500:
                backoff = 2 ** attempt
                if time.time() + backoff >= deadline:
                    break
                await asyncio.sleep(backoff)

        # 所有尝试失败，返回缓存（若有）
        if cached_data:
            logger.warning(f"[OWAPI] 所有尝试失败，返回缓存数据: {url}")
            return cached_data, ""
        return None, "请求失败（可能是服务器超时或故障），建议稍后重试"

    def _format_tag(self, tag: str) -> str:
        """格式化玩家标签（#替换为-）"""
        return tag.replace("#", "-")

    async def get_summary(self, tag: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """获取玩家概要信息（段位等）"""
        formatted_tag = self._format_tag(tag)
        url = f"{OW_API}/players/{formatted_tag}/summary"
        return await self._get(url, CACHE_TTL["summary"])

    async def get_mode_summary(self, tag: str, gamemode: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """获取指定模式的统计信息"""
        formatted_tag = self._format_tag(tag)
        url = f"{OW_API}/players/{formatted_tag}/stats/summary?gamemode={gamemode}"
        ttl_key = "comp_summary" if gamemode == "competitive" else "qp_summary"
        return await self._get(url, CACHE_TTL[ttl_key])

    async def get_hero_stats(self, tag: str, hero_key: str, gamemode: str = DEFAULT_MODE) -> Tuple[Optional[Dict[str, Any]], str]:
        """获取指定英雄的详细数据（默认休闲模式）"""
        formatted_tag = self._format_tag(tag)
        url = f"{OW_API}/players/{formatted_tag}/stats/career?gamemode={gamemode}&hero={hero_key}"
        return await self._get(url, CACHE_TTL["hero_stats"])

    def search_hero_key(self, hero_name: str) -> Optional[str]:
        """根据英雄中文名查找hero_key（不区分大小写）"""
        hero_name_lower = hero_name.strip().lower()
        for name, key in HERO_NAME_TO_KEY.items():
            if name.lower() == hero_name_lower or key == hero_name_lower:
                return key
        return None

# ---------- 格式化工具 ----------
class FormatTool:
    """数据格式化工具类（默认休闲模式）"""
    @staticmethod
    def format_division(div: Optional[str], tier: Optional[int]) -> str:
        """格式化段位显示（含分数范围）"""
        if not div or tier is None:
            return "未定级"
        cn_name = DIVISION_CN.get(div, div.upper())
        score_range = DIVISION_SCORE.get(div, "未知分数")
        return f"{cn_name} {tier} ({score_range}分)"

    @staticmethod
    def format_duration(sec: int) -> str:
        """格式化时长（秒转时分）"""
        h, m = divmod(sec // 60, 60)
        return f"{h}小时{m}分钟"

    @staticmethod
    def format_mode_stats(general: Dict[str, Any], mode_name: str) -> str:
        """格式化模式统计数据"""
        gp = general.get("games_played", 0)
        gw = general.get("games_won", 0)
        gl = gp - gw
        wr = (gw / gp * 100) if gp else 0.0
        kda = general.get("kda", 0)
        avg = general.get("average", {}) or {}
        
        return (
            f"【{mode_name}模式】\n"
            f"📊 总场次: {gp} | 胜: {gw} | 负: {gl} | 胜率: {wr:.1f}%\n"
            f"🎯 综合KD: {kda:.2f}\n"
            f"⚔️ 每10分钟平均:\n"
            f"　消灭: {avg.get('eliminations', 0):.1f} | "
            f"死亡: {avg.get('deaths', 0):.1f}\n"
            f"　伤害: {avg.get('damage', 0):.0f} | "
            f"治疗: {avg.get('healing', 0):.0f}"
        )

    @staticmethod
    def format_hero_stats(hero_data: Dict[str, Any], hero_name: str, hero_key: str, gamemode_cn: str) -> str:
        """格式化英雄详细数据（标注模式）"""
        hero_stats = hero_data.get(hero_key, {}) or {}
        combat = hero_stats.get("combat", {}) or {}
        average = hero_stats.get("average", {}) or {}
        best = hero_stats.get("best", {}) or {}
        game = hero_stats.get("game", {}) or {}
        
        total_games = game.get("games_played", 0)
        games_won = game.get("games_won", 0)
        win_rate = (games_won / total_games * 100) if total_games > 0 else 0.0
        total_elim = combat.get("eliminations", 0)
        total_damage = combat.get("hero_damage_done", 0)
        total_deaths = combat.get("deaths", 0)
        total_final_blows = combat.get("final_blows", 0)
        avg_elim = average.get("eliminations_avg_per_10_min", 0)
        avg_damage = average.get("hero_damage_done_avg_per_10_min", 0)
        avg_deaths = average.get("deaths_avg_per_10_min", 0)
        avg_final_blows = average.get("final_blows_avg_per_10_min", 0)
        best_elim = best.get("eliminations_most_in_game", 0)
        best_streak = best.get("kill_streak_best", 0)
        best_damage = best.get("hero_damage_done_most_in_game", 0)
        best_multikill = best.get("multikill_best", 0)
        
        return (
            f"【{hero_name} {gamemode_cn}模式数据】\n"
            f"📊 总场次: {total_games} | 胜场: {games_won} | 胜率: {win_rate:.1f}%\n"
            f"⚔️ 战斗统计:\n"
            f"　总消灭: {total_elim} | 总伤害: {total_damage:.0f}\n"
            f"　总死亡: {total_deaths} | 总最终一击: {total_final_blows}\n"
            f"🎯 每10分钟平均:\n"
            f"　消灭: {avg_elim:.1f} | 伤害: {avg_damage:.0f}\n"
            f"　死亡: {avg_deaths:.1f} | 最终一击: {avg_final_blows:.1f}\n"
            f"🏆 最佳表现:\n"
            f"　单局最高消灭: {best_elim} | 最长连杀: {best_streak}\n"
            f"　单局最高伤害: {best_damage:.0f} | 最佳多杀: {best_multikill}"
        )

# ---------- 插件主类（默认休闲模式） ----------
@register("astrbot_plugin_owcx", "tzyc", "国际服 OW2 数据查询", "v1.2.1")
class OWStatsPlugin(Star):
    def __init__(self,** kwargs):
        super().__init__(kwargs.get("context"))
        self.client = OWAPIClient()
        self.format_tool = FormatTool()
        # 绑定文件管理
        self.bind_file = Path("data/ow_stats_bind.json")
        self.bind_file.parent.mkdir(parents=True, exist_ok=True)
        self.bind_data = self._load_bind_data()

    # ---------- 绑定数据管理 ----------
    def _load_bind_data(self) -> Dict[str, str]:
        """加载绑定数据"""
        if self.bind_file.exists():
            try:
                return json.loads(self.bind_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"加载绑定数据失败: {str(e)}")
        return {}

    def _save_bind_data(self):
        """保存绑定数据"""
        try:
            self.bind_file.write_text(
                json.dumps(self.bind_data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"保存绑定数据失败: {str(e)}")

    # ---------- 核心命令（默认休闲模式） ----------
    @filter.command("ow")
    async def ow_stats_query(self, event: AstrMessageEvent):
        """战绩查询主命令（含竞技+休闲）"""
        args = event.message_str.strip().removeprefix("ow").strip().split()
        qq = str(event.get_sender_id())
        tag = ""
        platform = "pc"  # 默认PC平台
        
        # 解析参数
        if len(args) == 0:
            tag = self.bind_data.get(qq)
            if not tag:
                yield event.plain_result("请先绑定账号或直接查询：\n/ow 玩家#12345 [pc/console]")
                return
        elif len(args) == 1:
            tag = args[0]
        elif len(args) == 2 and args[1] in ["pc", "console"]:
            tag = args[0]
            platform = args[1]
        else:
            yield event.plain_result("参数格式错误！\n正确格式：/ow 玩家#12345 [pc/console]")
            return
        
        if "#" not in tag:
            yield event.plain_result("玩家标签格式错误！\n示例：/ow 玩家#12345")
            return
        
        yield event.plain_result(f"🔍 正在查询 {tag}（{platform}平台）...")
        
        try:
            # 并行请求竞技+休闲数据
            summary_task = self.client.get_summary(tag)
            comp_task = self.client.get_mode_summary(tag, "competitive")
            qp_task = self.client.get_mode_summary(tag, "quickplay")
            
            summary, summary_err = await summary_task
            comp_stats, comp_err = await comp_task
            qp_stats, qp_err = await qp_task
            
            if summary_err:
                yield event.plain_result(f"❌ {summary_err}")
                return
            
            # 解析段位+格式化数据
            role_lines = self._parse_division_data(summary, platform)
            season_hint = self._get_season_hint(summary, platform, comp_stats)
            comp_block = self._format_mode_block(comp_stats, comp_err, "竞技")
            qp_block = self._format_mode_block(qp_stats, qp_err, "休闲")
            
            result_msg = (
                f"🏆 【{tag}】亚服 OW2 战绩汇总\n"
                f"📱 平台: {'电脑端' if platform == 'pc' else '主机端'}\n"
                f"段位信息 | {' | '.join(role_lines)}\n"
                f"{season_hint}\n"
                f"{comp_block}\n\n{qp_block}"
            )
            
            yield event.plain_result(result_msg)
            
        except Exception as e:
            logger.error(f"查询异常: {str(e)}", exc_info=True)
            yield event.plain_result("❌ 查询异常，请稍后重试")

    @filter.command("ow英雄")
    async def ow_hero_stats(self, event: AstrMessageEvent):
        """英雄详细数据查询（默认休闲模式+错误优化）"""
        args = event.message_str.strip().removeprefix("ow英雄").strip().split()
        qq = str(event.get_sender_id())
        tag = ""
        hero_name = ""
        gamemode = DEFAULT_MODE
        gamemode_cn = DEFAULT_MODE_CN

        # 步骤1：解析模式参数
        if len(args) >= 1 and args[-1] in MODE_CN_TO_EN.keys():
            gamemode = MODE_CN_TO_EN[args[-1]]
            gamemode_cn = args[-1]
            args = args[:-1]

        # 步骤2：解析英雄名和玩家标签
        if len(args) >= 2 and "#" in args[-1]:
            hero_name = " ".join(args[:-1])
            tag = args[-1]
        elif len(args) == 1:
            hero_name = args[0]
            tag = self.bind_data.get(qq)
            if not tag:
                yield event.plain_result(
                    f"请先绑定账号或指定查询：\n"
                    f"1. 已绑定：/ow英雄 英雄名 [竞技/休闲]（默认{DEFAULT_MODE_CN}）\n"
                    f"2. 未绑定：/ow英雄 英雄名 玩家#12345 [竞技/休闲]"
                )
                return
        else:
            yield event.plain_result(
                "参数格式错误！\n正确格式：\n"
                f"1. 已绑定：/ow英雄 英雄名 [竞技/休闲]（默认{DEFAULT_MODE_CN}）\n"
                f"2. 未绑定：/ow英雄 英雄名 玩家#12345 [竞技/休闲]\n"
                f"示例：/ow英雄 源氏（默认休闲）| /ow英雄 源氏 竞技"
            )
            return
        
        # 步骤3：查找英雄key
        hero_key = self.client.search_hero_key(hero_name)
        if not hero_key:
            yield event.plain_result(f"❌ 未找到英雄：{hero_name}\n支持英雄：{', '.join(HERO_NAME_TO_KEY.keys())}")
            return
        
        # 步骤4：请求数据
        logger.info(f"[OW英雄查询] tag={tag}, hero={hero_name}, mode={gamemode_cn}")
        yield event.plain_result(f"🔍 正在查询 {tag} 的 {hero_name} {gamemode_cn}模式数据...")
        hero_data, err_msg = await self.client.get_hero_stats(tag, hero_key, gamemode)
        
        # 步骤5：错误处理（区分超时和其他错误）
        if err_msg:
            # 超时场景提示优化
            if "请求超时" in err_msg:
                err_msg += f"\n💡 提示：服务器响应较慢，可1分钟后再试，或切换竞技模式（/ow英雄 {hero_name} 竞技）"
            elif gamemode == "quickplay" and "服务器暂时无法处理请求" in err_msg:
                err_msg += f"\n💡 备选方案：尝试查询 {hero_name} 竞技模式，命令：/ow英雄 {hero_name} 竞技"
            logger.error(f"[OW英雄查询失败] tag={tag}, hero={hero_name}, mode={gamemode_cn} | 错误: {err_msg}")
            yield event.plain_result(f"❌ {err_msg}")
            return
        if not hero_data:
            empty_msg = f"❌ 未查询到 {hero_name} 的 {gamemode_cn}模式数据"
            if gamemode == "quickplay":
                empty_msg += f"\n💡 可尝试查询竞技模式：/ow英雄 {hero_name} 竞技"
            yield event.plain_result(empty_msg)
            return
        
        # 步骤6：数据判空与格式化
        hero_specific_data = hero_data.get(hero_key, {}) or {}
        game_stats = hero_specific_data.get("game", {}) or {}
        total_games = game_stats.get("games_played", 0)
        combat_stats = hero_specific_data.get("combat", {}) or {}
        has_combat_data = any(key in combat_stats for key in ["eliminations", "hero_damage_done"])
        
        if total_games == 0 and not has_combat_data:
            no_data_msg = (
                f"✅ API请求成功（状态码200）\n"
                f"❌ {tag} 未使用 {hero_name} 参与{gamemode_cn}模式对战\n"
                f"（提示：场次为0，无战斗数据）"
            )
            if gamemode == "quickplay":
                no_data_msg += f"\n💡 可尝试查询竞技模式：/ow英雄 {hero_name} 竞技"
            yield event.plain_result(no_data_msg)
            return
        elif total_games > 0 and not has_combat_data:
            yield event.plain_result(
                f"✅ API请求成功（状态码200）\n"
                f"⚠️ {tag} 使用 {hero_name} 参与{total_games}场{gamemode_cn}模式对战\n"
                f"❌ 暂未获取到该英雄的战斗数据（可能数据未同步）"
            )
            return
        
        # 步骤7：输出结果
        hero_msg = self.format_tool.format_hero_stats(hero_data, hero_name, hero_key, gamemode_cn)
        yield event.plain_result(hero_msg)

    # ---------- 绑定管理命令 ----------
    @filter.command("ow绑定")
    async def ow_bind_account(self, event: AstrMessageEvent):
        """绑定玩家账号（提示默认休闲）"""
        arg = event.message_str.strip().removeprefix("ow绑定").strip()
        qq = str(event.get_sender_id())
        
        if not arg or "#" not in arg:
            yield event.plain_result("绑定格式错误！\n正确格式：/ow绑定 玩家#12345")
            return
        
        self.bind_data[qq] = arg
        self._save_bind_data()
        yield event.plain_result(
            f"✅ 成功绑定账号：{arg}\n"
            f"📌 后续查询默认{DEFAULT_MODE_CN}模式：\n"
            f"　- 查战绩：/ow\n"
            f"　- 查英雄：/ow英雄 英雄名（如/ow英雄 源氏）\n"
            f"　- 查竞技：/ow英雄 英雄名 竞技"
        )

    @filter.command("ow解绑")
    async def ow_unbind_account(self, event: AstrMessageEvent):
        """解绑玩家账号"""
        qq = str(event.get_sender_id())
        if qq not in self.bind_data:
            yield event.plain_result("❌ 您尚未绑定任何账号")
            return
        
        old_tag = self.bind_data.pop(qq)
        self._save_bind_data()
        yield event.plain_result(f"✅ 成功解绑账号：{old_tag}")

    # ---------- 管理员专属命令 ----------
    @filter.command("ow清理缓存")
    @filter.permission_type(PermissionType.ADMIN)
    async def ow_clear_cache(self, event: AstrMessageEvent):
        """清理缓存（仅管理员）"""
        args = event.message_str.strip().removeprefix("ow清理缓存").strip()
        cache_size = self.client.cache.size()
        
        if args == "全部":
            self.client.cache.clear()
            yield event.plain_result(f"✅ 已清理全部缓存（共{cache_size}条）")
        else:
            self.client.cache.clear("players")
            yield event.plain_result(f"✅ 已清理玩家数据缓存（共{cache_size}条）")

    # ---------- 帮助与状态命令 ----------
    @filter.command("ow帮助")
    async def ow_help(self, event: AstrMessageEvent):
        """显示帮助信息（默认休闲模式）"""
        help_msg = (
            f"🎮 守望先锋2 亚服战绩查询插件（v1.2.1）\n"
            f"==============================\n"
            f"📌 说明：默认查询{DEFAULT_MODE_CN}模式，可显式指定“竞技”切换\n"
            f"🔍 基础查询：\n"
            f"  /ow 玩家#12345 [pc/console] - 查指定玩家战绩（含竞技+休闲）\n"
            f"  /ow - 查已绑定账号战绩\n"
            f"\n"
            f"🦸 英雄查询（默认{DEFAULT_MODE_CN}）：\n"
            f"  1. 已绑定账号：/ow英雄 英雄名 [竞技/休闲]\n"
            f"     示例：/ow英雄 源氏（默认休闲）| /ow英雄 源氏 竞技\n"
            f"  2. 未绑定账号：/ow英雄 英雄名 玩家#12345 [竞技/休闲]\n"
            f"     示例：/ow英雄 安娜 玩家#12345 休闲\n"
            f"\n"
            f"🔧 账号管理：\n"
            f"  /ow绑定 玩家#12345 - 绑定账号\n"
            f"  /ow解绑 - 解绑账号\n"
            f"\n"
            f"💡 管理员命令：\n"
            f"  /ow清理缓存 [全部] - 清理查询缓存\n"
            f"📌 提示：若{DEFAULT_MODE_CN}模式超时，可延长等待或切换竞技模式"
        )
        yield event.plain_result(help_msg)

    @filter.command("ow状态")
    async def ow_status(self, event: AstrMessageEvent):
        """显示插件状态（默认模式标注）"""
        test_data, _ = await self.client.get_summary("TeKrop-2217")
        api_status = "✅ 正常" if test_data else "❌ 异常"
        
        status_msg = (
            "🔧 守望先锋插件状态\n"
            "==================\n"
            f"API 连通性: {api_status}\n"
            f"已绑定账号: {len(self.bind_data)} 个\n"
            f"缓存数据量: {self.client.cache.size()} 条\n"
            f"插件版本: v1.2.1\n"
            f"默认模式: {DEFAULT_MODE_CN}（英雄查询默认）\n"
            f"超时配置: 60秒（减少超时概率）\n"
            f"支持功能: 基础战绩查询、英雄数据查询（竞技+休闲）\n"
            f"支持英雄数: {len(HERO_NAME_TO_KEY)} 个"
        )
        yield event.plain_result(status_msg)

    # ---------- 内部工具方法 ----------
    def _parse_division_data(self, summary: Dict[str, Any], platform: str) -> List[str]:
        """解析段位数据（双重判空）"""
        competitive = summary.get("competitive", {}) or {}
        platform_data = competitive.get(platform, {}) or {}
        
        role_lines = []
        for role in ["tank", "damage", "support"]:
            role_data = platform_data.get(role, {}) or {}
            div = role_data.get("division")
            tier = role_data.get("tier")
            role_cn = ROLE_CN[role]
            role_lines.append(f"{role_cn}: {self.format_tool.format_division(div, tier)}")
        
        return role_lines

    def _get_season_hint(self, summary: Dict[str, Any], platform: str, comp_stats: Optional[Dict[str, Any]]) -> str:
        """获取上赛季段位提示"""
        competitive = summary.get("competitive", {}) or {}
        platform_data = competitive.get(platform, {}) or {}
        
        season_lines = []
        comp_gp = comp_stats.get("general", {}).get("games_played", 0) if (comp_stats and comp_stats.get("general")) else 0
        if comp_gp == 0:
            for role in ["tank", "damage", "support"]:
                role_data = platform_data.get(role, {}) or {}
                if role_data.get("season") and role_data.get("division") and role_data.get("tier") is not None:
                    div = role_data["division"]
                    tier = role_data["tier"]
                    season = role_data["season"]
                    role_cn = ROLE_CN[role]
                    season_lines.append(f"{role_cn}: {self.format_tool.format_division(div, tier)} (S{season})")
        
        return f"📌 上赛季段位 | {' | '.join(season_lines)}\n" if season_lines else ""

    def _format_mode_block(self, stats: Optional[Dict[str, Any]], err_msg: str, mode_name: str) -> str:
        """格式化模式数据块"""
        if err_msg:
            return f"【{mode_name}模式】\n❌ {err_msg}"
        if not stats:
            return f"【{mode_name}模式】\n📊 暂无对战数据"
        general_stats = stats.get("general", {}) or {}
        total_games = general_stats.get("games_played", 0)
        if total_games == 0:
            return f"【{mode_name}模式】\n📊 未参与过该模式对战"
        
        return self.format_tool.format_mode_stats(general_stats, mode_name)

    async def terminate(self):
        """插件卸载时保存数据"""
        logger.info("OW2插件正在卸载，保存绑定数据...")
        self._save_bind_data()
        logger.info("OW2插件卸载完成")
