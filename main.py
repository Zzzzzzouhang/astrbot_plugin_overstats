from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import httpx
import json
from typing import Optional, Dict, Any, List, Tuple

class OverstatsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.base_url = "http://127.0.0.1:18080/api/v2"
        
        # 不同接口的超时配置
        self.timeouts = {
            "default": 30,
            "summary_yesterday": 45,
            "summary_week": 90,
            "auto_route": 60
        }
        
        # 初始化HTTP客户端
        self.clients = {}
        for key, timeout in self.timeouts.items():
            self.clients[key] = httpx.AsyncClient(timeout=timeout)
        
        logger.info("Overstats 守望先锋数据插件已加载")

    async def terminate(self):
        """插件退出时清理资源"""
        for client in self.clients.values():
            await client.aclose()
        logger.info("Overstats 守望先锋数据插件已卸载")

    def _get_client(self, endpoint: str) -> httpx.AsyncClient:
        """根据端点获取合适超时的客户端"""
        if "summary/week" in endpoint:
            return self.clients["summary_week"]
        elif "summary/yesterday" in endpoint:
            return self.clients["summary_yesterday"]
        elif "auto-route" in endpoint:
            return self.clients["auto_route"]
        else:
            return self.clients["default"]

    async def _api_request(self, endpoint: str, payload: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """通用API请求方法（JSON响应）"""
        try:
            url = f"{self.base_url}{endpoint}"
            client = self._get_client(endpoint)
            
            # 添加详细的请求日志
            logger.info(f"API请求: {url}, 载荷: {json.dumps(payload or {}, ensure_ascii=False)}")
            
            response = await client.post(url, json=payload or {})
            
            if response.status_code == 200:
                result = response.json()
                if result.get("ok"):
                    return result
                else:
                    error_msg = f"API错误: {result.get('error', '未知错误')}"
                    if result.get("message"):
                        error_msg += f" - {result['message']}"
                    if result.get("hint"):
                        error_msg += f"\n提示: {result['hint']}"
                    logger.error(error_msg)
                    return {"error": error_msg}
            else:
                logger.error(f"API请求失败: {url}, 状态码: {response.status_code}, 响应: {response.text[:200]}")
                return {"error": f"请求失败，状态码: {response.status_code}"}
        except Exception as e:
            logger.error(f"请求API时发生错误: {str(e)}", exc_info=True)
            return {"error": f"网络错误: {str(e)}"}

    async def _fetch_image(self, endpoint: str, payload: Dict[str, Any] = None) -> Optional[bytes]:
        """通用图片获取方法"""
        try:
            url = f"{self.base_url}{endpoint}"
            client = self._get_client(endpoint)
            
            # 添加详细的请求日志
            logger.info(f"图片请求: {url}, 载荷: {json.dumps(payload or {}, ensure_ascii=False)}")
            
            response = await client.post(url, json=payload or {})
            
            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if content_type.startswith("image/"):
                    return response.content
                else:
                    # 尝试解析JSON错误
                    try:
                        result = response.json()
                        if not result.get("ok"):
                            error_msg = f"API错误: {result.get('error', '未知错误')}"
                            if result.get("message"):
                                error_msg += f" - {result['message']}"
                            logger.error(error_msg)
                            return {"error": error_msg}
                    except:
                        logger.error(f"API返回非图片内容: {content_type}, 响应: {response.text[:200]}")
                        return {"error": "API返回了非图片内容"}
            else:
                logger.error(f"图片请求失败: {url}, 状态码: {response.status_code}, 响应: {response.text[:200]}")
                return {"error": f"请求失败，状态码: {response.status_code}"}
        except Exception as e:
            logger.error(f"请求图片时发生错误: {str(e)}", exc_info=True)
            return {"error": f"网络错误: {str(e)}"}

    async def _execute_image_command(self, event: AstrMessageEvent, endpoint: str, payload: Dict[str, Any] = None):
        """发送图片结果方法"""
        result = await self._fetch_image(endpoint, payload)
        if isinstance(result, bytes):
            return event.image_result(result)
        elif isinstance(result, dict) and "error" in result:
            return event.plain_result(result["error"])
        else:
            return event.plain_result("获取图片失败，请稍后再试")

    async def _execute_replies_command(self, event: AstrMessageEvent, endpoint: str, payload: Dict[str, Any] = None):
        """处理并发送所有回复内容"""
        result = await self._api_request(endpoint, payload)
        if not result or "error" in result:
            return event.plain_result(result.get("error", "获取数据失败") if result else "获取数据失败")
        
        if "replies" not in result:
            return event.plain_result("API返回格式错误")
        
        for reply in result["replies"]:
            if reply["type"] == "text":
                return event.plain_result(reply["data"])
            elif reply["type"] == "image":
                if "base64" in reply:
                    return event.image_result(base64_data=reply["base64"])
                elif "url" in reply:
                    return event.image_result(url=reply["url"])
            elif reply["type"] == "audio":
                return event.plain_result(f"[音频内容: {reply.get('media_type', 'audio')}]")
        return event.plain_result("未收到有效的回复内容")

    def _parse_bnet_id_from_args(self, args: List[str]) -> Optional[str]:
        """从参数列表中解析战网ID，处理包含空格的情况"""
        if not args:
            return None
        
        # 查找包含#的参数
        for i, arg in enumerate(args):
            if "#" in arg:
                bnet_parts = [arg]
                for j in range(i+1, len(args)):
                    if args[j].isdigit() or args[j] in ["show_all", "all", "analyze", "ai"]:
                        break
                    bnet_parts.append(args[j])
                
                bnet_id = " ".join(bnet_parts)
                logger.info(f"解析到战网ID: {bnet_id}")
                return bnet_id
        
        return " ".join(args)

    # ==================== 调试命令 ====================
    @filter.command("ow-debug", aliases=["守望调试"])
    async def cmd_debug(self, event: AstrMessageEvent, args: list = None):
        """调试命令，显示参数信息"""
        args = args or []
        debug_info = f"""
🔍 调试信息:
args: {args}
len(args): {len(args)}
event.command: {event.command}
        """
        return event.plain_result(debug_info.strip())

    # ==================== 大神资料 ====================
    @filter.command("profile", aliases=["大神资料", "资料", "p"])
    async def cmd_profile(self, event: AstrMessageEvent, args: list = None):
        """获取玩家资料: /profile [战网ID]"""
        args = args or []
        bnet_id = self._parse_bnet_id_from_args(args)
        if not bnet_id:
            return event.plain_result("请提供战网ID，例如: /profile 海盐冰淇淋#5911")
        
        payload = {"bnet_id": bnet_id}
        return await self._execute_image_command(event, "/dashen-profile/image", payload)

    # ==================== 大神对局 ====================
    @filter.command("match", aliases=["大神对局", "对局", "m"])
    async def cmd_match(self, event: AstrMessageEvent, args: list = None):
        """获取玩家最近对局: /match [战网ID] [数量]"""
        args = args or []
        bnet_id = self._parse_bnet_id_from_args(args)
        limit = 12
        
        if args and args[-1].isdigit():
            limit = int(args[-1])
            bnet_id = self._parse_bnet_id_from_args(args[:-1])
        
        if not bnet_id:
            return event.plain_result("请提供战网ID，例如: /match 海盐冰淇淋#5911")
        
        payload = {"bnet_id": bnet_id, "limit": limit}
        return await self._execute_replies_command(event, "/dashen-match/replies", payload)

    @filter.command("match-detail", aliases=["对局详情", "md"])
    async def cmd_match_detail(self, event: AstrMessageEvent, args: list = None):
        """获取对局详情: /match-detail [战网ID] [序号] [show_all] [analyze]"""
        args = args or []
        bnet_id = self._parse_bnet_id_from_args(args)
        index = 1
        show_all = False
        analyze = False
        
        if bnet_id:
            bnet_parts = bnet_id.split()
            remaining_args = args[len(bnet_parts):]
            
            if remaining_args:
                if remaining_args[0].isdigit():
                    index = int(remaining_args[0])
                    remaining_args = remaining_args[1:]
                
                if "show_all" in remaining_args or "all" in remaining_args:
                    show_all = True
                if "analyze" in remaining_args or "ai" in remaining_args:
                    analyze = True
        
        if not bnet_id:
            return event.plain_result("请提供战网ID和对局序号，例如: /match-detail 海盐冰淇淋#5911 1")
        
        payload = {
            "bnet_id": bnet_id, 
            "index": index,
            "show_all_heroes": show_all,
            "analyze": analyze
        }
        return await self._execute_replies_command(event, "/dashen-match/detail/replies", payload)

    # ==================== 大神同玩 ====================
    @filter.command("sameplay", aliases=["大神同玩", "同玩", "sp"])
    async def cmd_sameplay(self, event: AstrMessageEvent, args: list = None):
        """查询两个玩家的共同对局: /sameplay [玩家1] [玩家2]"""
        args = args or []
        player1, player2 = None, None
        for i, arg in enumerate(args):
            if "#" in arg:
                player1_parts = [arg]
                for j in range(i+1, len(args)):
                    if "#" in args[j]:
                        player2_parts = [args[j]]
                        for k in range(j+1, len(args)):
                            if args[k].isdigit() or args[k] in ["show_all", "all", "analyze", "ai"]:
                                break
                            player2_parts.append(args[k])
                        player2 = " ".join(player2_parts)
                        break
                    player1_parts.append(args[j])
                player1 = " ".join(player1_parts)
                break
        
        if not player1 or not player2:
            return event.plain_result("请提供两个战网ID，例如: /sameplay 海盐冰淇淋#5911 Player#12345")
        
        payload = {"player1_bnet_id": player1, "player2_bnet_id": player2}
        return await self._execute_replies_command(event, "/dashen-sameplay/replies", payload)

    @filter.command("sameplay-detail", aliases=["同玩详情", "spd"])
    async def cmd_sameplay_detail(self, event: AstrMessageEvent, args: list = None):
        """获取同玩对局详情: /sameplay-detail [玩家1] [玩家2] [序号] [show_all] [analyze]"""
        args = args or []
        player1, player2 = None, None
        index = 1
        show_all, analyze = False, False
        
        for i, arg in enumerate(args):
            if "#" in arg:
                player1_parts = [arg]
                for j in range(i+1, len(args)):
                    if "#" in args[j]:
                        player2_parts = [args[j]]
                        remaining_args = []
                        for k in range(j+1, len(args)):
                            player2_parts.append(args[k])
                            if args[k].isdigit() or args[k] in ["show_all", "all", "analyze", "ai"]:
                                remaining_args = args[k+1:]
                                break
                        player2 = " ".join(player2_parts)
                        
                        if remaining_args:
                            if remaining_args[0].isdigit():
                                index = int(remaining_args[0])
                                remaining_args = remaining_args[1:]
                            if "show_all" in remaining_args or "all" in remaining_args:
                                show_all = True
                            if "analyze" in remaining_args or "ai" in remaining_args:
                                analyze = True
                        break
                    player1_parts.append(args[j])
                player1 = " ".join(player1_parts)
                break
        
        if not player1 or not player2:
            return event.plain_result("请提供两个战网ID和对局序号，例如: /sameplay-detail 海盐冰淇淋#5911 Player#12345 1")
        
        payload = {
            "player1_bnet_id": player1,
            "player2_bnet_id": player2,
            "index": index,
            "show_all_heroes": show_all,
            "analyze": analyze
        }
        return await self._execute_replies_command(event, "/dashen-sameplay/detail/replies", payload)

    # ==================== 排名历史 ====================
    @filter.command("rank-history", aliases=["排名历史", "rh"])
    async def cmd_rank_history(self, event: AstrMessageEvent, args: list = None):
        """获取玩家赛季排名历史: /rank-history [战网ID]"""
        args = args or []
        bnet_id = self._parse_bnet_id_from_args(args)
        if not bnet_id:
            return event.plain_result("请提供战网ID，例如: /rank-history 海盐冰淇淋#5911")
        
        payload = {"bnet_id": bnet_id}
        return await self._execute_image_command(event, "/dashen-rank-history/image", payload)

    # ==================== 实力分析 ====================
    @filter.command("quick-strength", aliases=["快速实力", "qs"])
    async def cmd_quick_strength(self, event: AstrMessageEvent, args: list = None):
        """获取快速模式实力分析: /quick-strength [战网ID] [数量]"""
        args = args or []
        bnet_id = self._parse_bnet_id_from_args(args)
        limit = 12
        if args and args[-1].isdigit():
            limit = int(args[-1])
            bnet_id = self._parse_bnet_id_from_args(args[:-1])
        
        if not bnet_id:
            return event.plain_result("请提供战网ID，例如: /quick-strength 海盐冰淇淋#5911")
        
        payload = {"bnet_id": bnet_id, "limit": limit}
        return await self._execute_image_command(event, "/dashen-quick-strength/image", payload)

    @filter.command("competitive-strength", aliases=["竞技实力", "cs", "cstrength"])
    async def cmd_competitive_strength(self, event: AstrMessageEvent, args: list = None):
        """获取竞技模式实力分析: /competitive-strength [战网ID] [数量]"""
        args = args or []
        bnet_id = self._parse_bnet_id_from_args(args)
        limit = 12
        if args and args[-1].isdigit():
            limit = int(args[-1])
            bnet_id = self._parse_bnet_id_from_args(args[:-1])
        
        if not bnet_id:
            return event.plain_result("请提供战网ID，例如: /competitive-strength 海盐冰淇淋#5911")
        
        payload = {"bnet_id": bnet_id, "limit": limit}
        return await self._execute_image_command(event, "/dashen-competitive-strength/image", payload)

    # ==================== 排行榜 ====================
    @filter.command("rank-leaderboard", aliases=["排名榜", "rl", "leaderboard"])
    async def cmd_rank_leaderboard(self, event: AstrMessageEvent, args: list = None):
        """获取地区排名榜: /rank-leaderboard [省份] [角色]"""
        args = args or []
        province = args[0] if len(args) >= 1 else "全国"
        role = args[1] if len(args) >= 2 else "all"
        payload = {"province": province, "role": role}
        return await self._execute_image_command(event, "/dashen-rank-leaderboard/image", payload)

    @filter.command("hero-leaderboard", aliases=["英雄榜", "hl", "hero"])
    async def cmd_hero_leaderboard(self, event: AstrMessageEvent, args: list = None):
        """获取英雄排行榜: /hero-leaderboard [英雄名] [省份]"""
        args = args or []
        if not args:
            return event.plain_result("请提供英雄名，例如: /hero-leaderboard 安娜")
        hero = args[0]
        province = args[1] if len(args) >= 2 else "全国"
        mode = args[2] if len(args) >= 3 else "preset"
        payload = {"hero": hero, "province": province, "mode": mode}
        return await self._execute_image_command(event, "/dashen-hero-leaderboard/image", payload)

    # ==================== 英雄数据 ====================
    @filter.command("hero-pick-rate", aliases=["英雄选取率", "pickrate", "pr"])
    async def cmd_hero_pick_rate(self, event: AstrMessageEvent, args: list = None):
        """获取英雄选取率: /hero-pick-rate [模式] [段位]"""
        args = args or []
        game_mode = args[0] if len(args) >= 1 else "competitive"
        mmr = args[1] if len(args) >= 2 else "all"
        payload = {"view": "ranking", "game_mode": game_mode, "mmr": mmr}
        return await self._execute_image_command(event, "/ow-hero-pick-rate/image", payload)

    @filter.command("hero-perk", aliases=["英雄威能", "perk"])
    async def cmd_hero_perk(self, event: AstrMessageEvent, args: list = None):
        """获取英雄威能数据: /hero-perk [英雄名]"""
        args = args or []
        if not args:
            return event.plain_result("请提供英雄名，例如: /hero-perk 安娜")
        hero = " ".join(args)
        payload = {"hero": hero}
        return await self._execute_image_command(event, "/ow-hero-perk/image", payload)

    # ==================== 每日总结 ====================
    @filter.command("today-summary", aliases=["今日总结", "today", "ts"])
    async def cmd_today_summary(self, event: AstrMessageEvent, args: list = None):
        """获取今日游戏总结: /today-summary [战网ID]"""
        args = args or []
        bnet_id = self._parse_bnet_id_from_args(args)
        if not bnet_id:
            return event.plain_result("请提供战网ID，例如: /today-summary 海盐冰淇淋#5911")
        
        payload = {"bnet_id": bnet_id}
        return await self._execute_image_command(event, "/dashen-summary/today/image", payload)

    @filter.command("yesterday-summary", aliases=["昨日总结", "yesterday", "ys"])
    async def cmd_yesterday_summary(self, event: AstrMessageEvent, args: list = None):
        """获取昨日游戏总结: /yesterday-summary [战网ID]"""
        args = args or []
        bnet_id = self._parse_bnet_id_from_args(args)
        if not bnet_id:
            return event.plain_result("请提供战网ID，例如: /yesterday-summary 海盐冰淇淋#5911")
        
        payload = {"bnet_id": bnet_id}
        return await self._execute_image_command(event, "/dashen-summary/yesterday/image", payload)

    @filter.command("week-summary", aliases=["本周总结", "week", "ws"])
    async def cmd_week_summary(self, event: AstrMessageEvent, args: list = None):
        """获取本周游戏总结: /week-summary [战网ID]"""
        args = args or []
        bnet_id = self._parse_bnet_id_from_args(args)
        if not bnet_id:
            return event.plain_result("请提供战网ID，例如: /week-summary 海盐冰淇淋#5911")
        
        payload = {"bnet_id": bnet_id}
        return await self._execute_image_command(event, "/dashen-summary/week/image", payload)

    # ==================== 电竞资讯 ====================
    @filter.command("esports", aliases=["电竞资讯", "电竞", "e"])
    async def cmd_esports(self, event: AstrMessageEvent, args: list = None):
        """获取最新电竞资讯"""
        return await self._execute_image_command(event, "/ow-esports/image")

    # ==================== 商店信息 ====================
    @filter.command("shop", aliases=["商店", "s"])
    async def cmd_shop(self, event: AstrMessageEvent, args: list = None):
        """获取当前商店信息"""
        return await self._execute_image_command(event, "/ow-shop/image")

    # ==================== 更新日志 ====================
    @filter.command("patch-notes", aliases=["更新日志", "patch", "pn"])
    async def cmd_patch_notes(self, event: AstrMessageEvent, args: list = None):
        """获取更新日志: /patch-notes [latest/small/big]"""
        args = args or []
        kind = " ".join(args) if args else "latest"
        payload = {"patch_kind": kind}
        return await self._execute_image_command(event, "/patch-notes/image", payload)

    # ==================== 猜英雄游戏 ====================
    @filter.command("guess", aliases=["猜英雄", "g"])
    async def cmd_guess(self, event: AstrMessageEvent, args: list = None):
        """开始猜英雄游戏: /guess [类型]"""
        args = args or []
        question_type = " ".join(args) if args else "hero_icon"
        payload = {"question_type": question_type}
        return await self._execute_replies_command(event, "/ow-guess/replies", payload)

    # ==================== 自然语言查询 ====================
    @filter.command("ow", aliases=["守望", "o"])
    async def cmd_auto_route(self, event: AstrMessageEvent, args: list = None):
        """自然语言查询: /ow 帮我看一下海盐冰淇淋#5911这周总结"""
        args = args or []
        text = " ".join(args)
        if not text:
            return event.plain_result("请输入查询内容，例如: /ow 帮我看一下海盐冰淇淋#5911的竞技实力")
        
        payload = {"text": text}
        return await self._execute_replies_command(event, "/auto-route", payload)

    # ==================== 帮助命令 ====================
    @filter.command("ow-help", aliases=["守望帮助", "oh"])
    async def cmd_help(self, event: AstrMessageEvent, args: list = None):
        """显示Overstats插件帮助"""
        help_text = """
🎮 Overstats 守望先锋数据插件帮助 🎮

📊 玩家查询:
/profile /资料 [战网ID] - 获取玩家资料
/match /对局 [战网ID] [数量] - 获取最近对局
/match-detail /md [战网ID] [序号] - 获取对局详情
/rank-history /rh [战网ID] - 获取赛季排名历史

⚔️ 实力分析:
/quick-strength /qs [战网ID] - 快速模式实力分析
/competitive-strength /cs [战网ID] - 竞技模式实力分析

👥 同玩查询:
/sameplay /sp [玩家1] [玩家2] - 查询共同对局
/sameplay-detail /spd [玩家1] [玩家2] [序号] - 同玩对局详情

📅 游戏总结:
/today-summary /today [战网ID] - 今日总结
/yesterday-summary /yesterday [战网ID] - 昨日总结
/week-summary /week [战网ID] - 本周总结

🏆 排行榜:
/rank-leaderboard /rl [省份] [角色] - 地区排名榜
/hero-leaderboard /hl [英雄名] [省份] - 英雄排行榜

📉 英雄数据:
/hero-pick-rate /pr [模式] [段位] - 英雄选取率
/hero-perk /perk [英雄名] - 英雄威能数据

🎬 其他功能:
/esports /电竞 - 最新电竞资讯
/shop /商店 - 当前商店信息
/patch-notes /patch - 更新日志
/guess /g [类型] - 猜英雄游戏

✨ 智能查询:
/ow /守望 [自然语言] - 自然语言查询

/ow-help /oh - 显示此帮助信息
        """
        return event.plain_result(help_text.strip())
