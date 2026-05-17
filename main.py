from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import httpx
import json
from typing import Optional, Dict, Any, List

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

    async def _send_image_result(self, event: AstrMessageEvent, endpoint: str, payload: Dict[str, Any] = None):
        """通用发送图片结果方法"""
        result = await self._fetch_image(endpoint, payload)
        if isinstance(result, bytes):
            yield event.image_result(result)
        elif isinstance(result, dict) and "error" in result:
            yield event.plain_result(result["error"])
        else:
            yield event.plain_result("获取图片失败，请稍后再试")

    async def _send_replies_result(self, event: AstrMessageEvent, endpoint: str, payload: Dict[str, Any] = None):
        """处理replies接口，自动发送所有回复内容"""
        result = await self._api_request(endpoint, payload)
        if not result or "error" in result:
            yield event.plain_result(result.get("error", "获取数据失败"))
            return
        
        if "replies" not in result:
            yield event.plain_result("API返回格式错误")
            return
        
        for reply in result["replies"]:
            if reply["type"] == "text":
                yield event.plain_result(reply["data"])
            elif reply["type"] == "image":
                if "base64" in reply:
                    yield event.image_result(base64_data=reply["base64"])
                elif "url" in reply:
                    yield event.image_result(url=reply["url"])
            elif reply["type"] == "audio":
                # AstrBot暂不支持直接发送音频，发送提示
                yield event.plain_result(f"[音频内容: {reply.get('media_type', 'audio')}]")

    def _get_raw_message(self, event: AstrMessageEvent) -> str:
        """获取原始消息内容（包含命令）"""
        try:
            # 尝试所有可能的方法获取原始消息
            if hasattr(event, 'raw_message'):
                return event.raw_message.strip()
            elif hasattr(event, 'get_raw_message'):
                return event.get_raw_message().strip()
            elif hasattr(event, 'message') and hasattr(event.message, 'raw_content'):
                return event.message.raw_content.strip()
            elif hasattr(event, 'get_message'):
                return event.get_message().strip()
            elif hasattr(event, 'content'):
                return event.content.strip()
            else:
                logger.error("无法获取原始消息内容")
                return ""
        except Exception as e:
            logger.error(f"获取原始消息时发生错误: {str(e)}", exc_info=True)
            return ""

    def _extract_args(self, event: AstrMessageEvent, command_aliases: List[str]) -> str:
        """从原始消息中提取命令参数部分"""
        raw_msg = self._get_raw_message(event)
        if not raw_msg:
            return ""
        
        # 记录原始消息用于调试
        logger.info(f"原始消息: {raw_msg}")
        
        # 尝试匹配所有命令别名
        for alias in command_aliases:
            # 支持带/和不带/的命令
            patterns = [f"/{alias} ", f"{alias} "]
            for pattern in patterns:
                if raw_msg.startswith(pattern):
                    args = raw_msg[len(pattern):].strip()
                    logger.info(f"匹配到命令 {pattern}, 参数: {args}")
                    return args
        
        # 如果没有匹配到，尝试去掉第一个单词（命令）
        parts = raw_msg.split(maxsplit=1)
        if len(parts) > 1:
            args = parts[1].strip()
            logger.info(f"使用回退方法提取参数: {args}")
            return args
        
        return ""

    def _parse_bnet_id(self, args: str) -> Optional[str]:
        """解析战网ID，处理特殊字符和空格问题"""
        if not args:
            return None
        
        # 移除多余的空格
        args = args.strip()
        
        # 如果包含#，直接返回
        if "#" in args:
            return args
        
        # 尝试从多个参数中拼接战网ID
        parts = args.split()
        for i in range(len(parts)):
            if "#" in parts[i]:
                # 战网ID可能被拆分成了多个部分
                return " ".join(parts[:i+1])
        
        return args

    # ==================== 大神资料 ====================
    @filter.command("profile", aliases=["大神资料", "资料", "p"])
    async def cmd_profile(self, event: AstrMessageEvent):
        """获取玩家资料: /profile [战网ID]"""
        args = self._extract_args(event, ["profile", "大神资料", "资料", "p"])
        bnet_id = self._parse_bnet_id(args)
        if not bnet_id:
            yield event.plain_result("请提供战网ID，例如: /profile 海盐冰淇淋#5911")
            return
        
        payload = {"bnet_id": bnet_id}
        async for result in self._send_image_result(event, "/dashen-profile/image", payload):
            yield result

    # ==================== 大神对局 ====================
    @filter.command("match", aliases=["大神对局", "对局", "m"])
    async def cmd_match(self, event: AstrMessageEvent):
        """获取玩家最近对局: /match [战网ID] [数量]"""
        args = self._extract_args(event, ["match", "大神对局", "对局", "m"])
        if not args:
            yield event.plain_result("请提供战网ID，例如: /match 海盐冰淇淋#5911")
            return
        
        parts = args.split()
        bnet_id = self._parse_bnet_id(args)
        limit = 12
        
        # 尝试解析数量参数
        if len(parts) > 1 and parts[-1].isdigit():
            limit = int(parts[-1])
            # 移除数量参数，重新解析战网ID
            bnet_id = self._parse_bnet_id(" ".join(parts[:-1]))
        
        if not bnet_id:
            yield event.plain_result("请提供战网ID，例如: /match 海盐冰淇淋#5911")
            return
        
        payload = {"bnet_id": bnet_id, "limit": limit}
        async for result in self._send_replies_result(event, "/dashen-match/replies", payload):
            yield result

    @filter.command("match-detail", aliases=["对局详情", "md"])
    async def cmd_match_detail(self, event: AstrMessageEvent):
        """获取对局详情: /match-detail [战网ID] [序号] [show_all] [analyze]"""
        args = self._extract_args(event, ["match-detail", "对局详情", "md"])
        if not args:
            yield event.plain_result("请提供战网ID和对局序号，例如: /match-detail 海盐冰淇淋#5911 1")
            return
        
        parts = args.split()
        bnet_id = self._parse_bnet_id(args)
        index = 1
        show_all = False
        analyze = False
        
        # 解析参数
        remaining = []
        for i, part in enumerate(parts):
            if "#" in part:
                bnet_id = " ".join(parts[:i+1])
                remaining = parts[i+1:]
                break
        
        if remaining:
            if remaining[0].isdigit():
                index = int(remaining[0])
                remaining = remaining[1:]
            
            if "show_all" in remaining or "all" in remaining:
                show_all = True
            if "analyze" in remaining or "ai" in remaining:
                analyze = True
        
        if not bnet_id:
            yield event.plain_result("请提供战网ID和对局序号，例如: /match-detail 海盐冰淇淋#5911 1")
            return
        
        payload = {
            "bnet_id": bnet_id, 
            "index": index,
            "show_all_heroes": show_all,
            "analyze": analyze
        }
        async for result in self._send_replies_result(event, "/dashen-match/detail/replies", payload):
            yield result

    # ==================== 大神同玩 ====================
    @filter.command("sameplay", aliases=["大神同玩", "同玩", "sp"])
    async def cmd_sameplay(self, event: AstrMessageEvent):
        """查询两个玩家的共同对局: /sameplay [玩家1] [玩家2]"""
        args = self._extract_args(event, ["sameplay", "大神同玩", "同玩", "sp"])
        if not args:
            yield event.plain_result("请提供两个战网ID，例如: /sameplay 海盐冰淇淋#5911 Player#12345")
            return
        
        # 分割两个战网ID
        parts = args.split()
        player1 = None
        player2 = None
        
        # 查找第一个#的位置
        for i, part in enumerate(parts):
            if "#" in part:
                player1 = " ".join(parts[:i+1])
                # 剩下的部分作为第二个玩家
                player2 = self._parse_bnet_id(" ".join(parts[i+1:]))
                break
        
        if not player1 or not player2:
            yield event.plain_result("请提供两个战网ID，例如: /sameplay 海盐冰淇淋#5911 Player#12345")
            return
        
        payload = {
            "player1_bnet_id": player1,
            "player2_bnet_id": player2
        }
        async for result in self._send_replies_result(event, "/dashen-sameplay/replies", payload):
            yield result

    @filter.command("sameplay-detail", aliases=["同玩详情", "spd"])
    async def cmd_sameplay_detail(self, event: AstrMessageEvent):
        """获取同玩对局详情: /sameplay-detail [玩家1] [玩家2] [序号] [show_all] [analyze]"""
        args = self._extract_args(event, ["sameplay-detail", "同玩详情", "spd"])
        if not args:
            yield event.plain_result("请提供两个战网ID和对局序号，例如: /sameplay-detail 海盐冰淇淋#5911 Player#12345 1")
            return
        
        parts = args.split()
        player1 = None
        player2 = None
        index = 1
        show_all = False
        analyze = False
        
        # 查找第一个#的位置
        for i, part in enumerate(parts):
            if "#" in part:
                player1 = " ".join(parts[:i+1])
                remaining = parts[i+1:]
                
                # 查找第二个#的位置
                for j, part2 in enumerate(remaining):
                    if "#" in part2:
                        player2 = " ".join(remaining[:j+1])
                        remaining2 = remaining[j+1:]
                        
                        # 解析剩余参数
                        if remaining2:
                            if remaining2[0].isdigit():
                                index = int(remaining2[0])
                                remaining2 = remaining2[1:]
                            
                            if "show_all" in remaining2 or "all" in remaining2:
                                show_all = True
                            if "analyze" in remaining2 or "ai" in remaining2:
                                analyze = True
                        
                        break
                break
        
        if not player1 or not player2:
            yield event.plain_result("请提供两个战网ID和对局序号，例如: /sameplay-detail 海盐冰淇淋#5911 Player#12345 1")
            return
        
        payload = {
            "player1_bnet_id": player1,
            "player2_bnet_id": player2,
            "index": index,
            "show_all_heroes": show_all,
            "analyze": analyze
        }
        async for result in self._send_replies_result(event, "/dashen-sameplay/detail/replies", payload):
            yield result

    # ==================== 排名历史 ====================
    @filter.command("rank-history", aliases=["排名历史", "rh"])
    async def cmd_rank_history(self, event: AstrMessageEvent):
        """获取玩家赛季排名历史: /rank-history [战网ID]"""
        args = self._extract_args(event, ["rank-history", "排名历史", "rh"])
        bnet_id = self._parse_bnet_id(args)
        if not bnet_id:
            yield event.plain_result("请提供战网ID，例如: /rank-history 海盐冰淇淋#5911")
            return
        
        payload = {"bnet_id": bnet_id}
        async for result in self._send_image_result(event, "/dashen-rank-history/image", payload):
            yield result

    # ==================== 实力分析 ====================
    @filter.command("quick-strength", aliases=["快速实力", "qs"])
    async def cmd_quick_strength(self, event: AstrMessageEvent):
        """获取快速模式实力分析: /quick-strength [战网ID] [数量]"""
        args = self._extract_args(event, ["quick-strength", "快速实力", "qs"])
        if not args:
            yield event.plain_result("请提供战网ID，例如: /quick-strength 海盐冰淇淋#5911")
            return
        
        parts = args.split()
        bnet_id = self._parse_bnet_id(args)
        limit = 12
        
        # 尝试解析数量参数
        if len(parts) > 1 and parts[-1].isdigit():
            limit = int(parts[-1])
            # 移除数量参数，重新解析战网ID
            bnet_id = self._parse_bnet_id(" ".join(parts[:-1]))
        
        if not bnet_id:
            yield event.plain_result("请提供战网ID，例如: /quick-strength 海盐冰淇淋#5911")
            return
        
        payload = {"bnet_id": bnet_id, "limit": limit}
        async for result in self._send_image_result(event, "/dashen-quick-strength/image", payload):
            yield result

    @filter.command("competitive-strength", aliases=["竞技实力", "cs", "cstrength"])
    async def cmd_competitive_strength(self, event: AstrMessageEvent):
        """获取竞技模式实力分析: /competitive-strength [战网ID] [数量]"""
        args = self._extract_args(event, ["competitive-strength", "竞技实力", "cs", "cstrength"])
        if not args:
            yield event.plain_result("请提供战网ID，例如: /competitive-strength 海盐冰淇淋#5911")
            return
        
        parts = args.split()
        bnet_id = self._parse_bnet_id(args)
        limit = 12
        
        # 尝试解析数量参数
        if len(parts) > 1 and parts[-1].isdigit():
            limit = int(parts[-1])
            # 移除数量参数，重新解析战网ID
            bnet_id = self._parse_bnet_id(" ".join(parts[:-1]))
        
        if not bnet_id:
            yield event.plain_result("请提供战网ID，例如: /competitive-strength 海盐冰淇淋#5911")
            return
        
        payload = {"bnet_id": bnet_id, "limit": limit}
        async for result in self._send_image_result(event, "/dashen-competitive-strength/image", payload):
            yield result

    # ==================== 排行榜 ====================
    @filter.command("rank-leaderboard", aliases=["排名榜", "rl", "leaderboard"])
    async def cmd_rank_leaderboard(self, event: AstrMessageEvent):
        """获取地区排名榜: /rank-leaderboard [省份] [角色(tank/dps/healer/open)]"""
        args = self._extract_args(event, ["rank-leaderboard", "排名榜", "rl", "leaderboard"])
        args_list = args.split() if args else []
        province = "全国"
        role = "all"
        
        if len(args_list) >= 1:
            province = args_list[0]
        if len(args_list) >= 2:
            role = args_list[1]
        
        payload = {"province": province, "role": role}
        async for result in self._send_image_result(event, "/dashen-rank-leaderboard/image", payload):
            yield result

    @filter.command("hero-leaderboard", aliases=["英雄榜", "hl", "hero"])
    async def cmd_hero_leaderboard(self, event: AstrMessageEvent):
        """获取英雄排行榜: /hero-leaderboard [英雄名] [省份] [模式(preset/open)]"""
        args = self._extract_args(event, ["hero-leaderboard", "英雄榜", "hl", "hero"])
        args_list = args.split() if args else []
        if not args_list:
            yield event.plain_result("请提供英雄名，例如: /hero-leaderboard 安娜")
            return
        
        hero = args_list[0]
        province = "全国"
        mode = "preset"
        
        if len(args_list) >= 2:
            province = args_list[1]
        if len(args_list) >= 3:
            mode = args_list[2]
        
        payload = {"hero": hero, "province": province, "mode": mode}
        async for result in self._send_image_result(event, "/dashen-hero-leaderboard/image", payload):
            yield result

    # ==================== 英雄数据 ====================
    @filter.command("hero-pick-rate", aliases=["英雄选取率", "pickrate", "pr"])
    async def cmd_hero_pick_rate(self, event: AstrMessageEvent):
        """获取英雄选取率: /hero-pick-rate [模式(quick/competitive)] [段位]"""
        args = self._extract_args(event, ["hero-pick-rate", "英雄选取率", "pickrate", "pr"])
        args_list = args.split() if args else []
        game_mode = "competitive"
        mmr = "all"
        
        if len(args_list) >= 1:
            game_mode = args_list[0]
        if len(args_list) >= 2:
            mmr = args_list[1]
        
        payload = {
            "view": "ranking",
            "game_mode": game_mode,
            "mmr": mmr
        }
        async for result in self._send_image_result(event, "/ow-hero-pick-rate/image", payload):
            yield result

    @filter.command("hero-perk", aliases=["英雄威能", "perk"])
    async def cmd_hero_perk(self, event: AstrMessageEvent):
        """获取英雄威能数据: /hero-perk [英雄名]"""
        hero = self._extract_args(event, ["hero-perk", "英雄威能", "perk"])
        if not hero:
            yield event.plain_result("请提供英雄名，例如: /hero-perk 安娜")
            return
        
        payload = {"hero": hero}
        async for result in self._send_image_result(event, "/ow-hero-perk/image", payload):
            yield result

    # ==================== 每日总结 ====================
    @filter.command("today-summary", aliases=["今日总结", "today", "ts"])
    async def cmd_today_summary(self, event: AstrMessageEvent):
        """获取今日游戏总结: /today-summary [战网ID]"""
        args = self._extract_args(event, ["today-summary", "今日总结", "today", "ts"])
        bnet_id = self._parse_bnet_id(args)
        if not bnet_id:
            yield event.plain_result("请提供战网ID，例如: /today-summary 海盐冰淇淋#5911")
            return
        
        payload = {"bnet_id": bnet_id}
        async for result in self._send_image_result(event, "/dashen-summary/today/image", payload):
            yield result

    @filter.command("yesterday-summary", aliases=["昨日总结", "yesterday", "ys"])
    async def cmd_yesterday_summary(self, event: AstrMessageEvent):
        """获取昨日游戏总结: /yesterday-summary [战网ID]"""
        args = self._extract_args(event, ["yesterday-summary", "昨日总结", "yesterday", "ys"])
        bnet_id = self._parse_bnet_id(args)
        if not bnet_id:
            yield event.plain_result("请提供战网ID，例如: /yesterday-summary 海盐冰淇淋#5911")
            return
        
        payload = {"bnet_id": bnet_id}
        async for result in self._send_image_result(event, "/dashen-summary/yesterday/image", payload):
            yield result

    @filter.command("week-summary", aliases=["本周总结", "week", "ws"])
    async def cmd_week_summary(self, event: AstrMessageEvent):
        """获取本周游戏总结: /week-summary [战网ID] (可能需要较长时间)"""
        args = self._extract_args(event, ["week-summary", "本周总结", "week", "ws"])
        bnet_id = self._parse_bnet_id(args)
        if not bnet_id:
            yield event.plain_result("请提供战网ID，例如: /week-summary 海盐冰淇淋#5911")
            return
        
        yield event.plain_result("正在生成本周总结，数据量较大，请稍候...")
        payload = {"bnet_id": bnet_id}
        async for result in self._send_image_result(event, "/dashen-summary/week/image", payload):
            yield result

    # ==================== 电竞资讯 ====================
    @filter.command("esports", aliases=["电竞资讯", "电竞", "e"])
    async def cmd_esports(self, event: AstrMessageEvent):
        """获取最新电竞资讯"""
        async for result in self._send_image_result(event, "/ow-esports/image"):
            yield result

    # ==================== 商店信息 ====================
    @filter.command("shop", aliases=["商店", "s"])
    async def cmd_shop(self, event: AstrMessageEvent):
        """获取当前商店信息"""
        async for result in self._send_image_result(event, "/ow-shop/image"):
            yield result

    # ==================== 更新日志 ====================
    @filter.command("patch-notes", aliases=["更新日志", "patch", "pn"])
    async def cmd_patch_notes(self, event: AstrMessageEvent):
        """获取更新日志: /patch-notes [latest/small/big]"""
        kind = self._extract_args(event, ["patch-notes", "更新日志", "patch", "pn"]) or "latest"
        payload = {"patch_kind": kind}
        async for result in self._send_image_result(event, "/patch-notes/image", payload):
            yield result

    # ==================== 猜英雄游戏 ====================
    @filter.command("guess", aliases=["猜英雄", "g"])
    async def cmd_guess(self, event: AstrMessageEvent):
        """开始猜英雄游戏: /guess [类型]
        支持类型: hero_icon(英雄图标), map_music(地图音乐), skill_icon_hero(技能图标),
        perk_icon_hero(威能图标), map_image(地图图片), ult_voice(大招语音),
        hero_silhouette(英雄剪影), skill_icon_name(技能名称), hero_description(英雄描述)"""
        
        question_type = self._extract_args(event, ["guess", "猜英雄", "g"]) or "hero_icon"
        payload = {"question_type": question_type}
        async for result in self._send_replies_result(event, "/ow-guess/replies", payload):
            yield result

    # ==================== 自然语言查询 ====================
    @filter.command("ow", aliases=["守望", "o"])
    async def cmd_auto_route(self, event: AstrMessageEvent):
        """自然语言查询: /ow 帮我看一下海盐冰淇淋#5911这周总结"""
        text = self._extract_args(event, ["ow", "守望", "o"])
        if not text:
            yield event.plain_result("请输入查询内容，例如: /ow 帮我看一下海盐冰淇淋#5911的竞技实力")
            return
        
        yield event.plain_result("正在处理您的查询...")
        payload = {"text": text}
        async for result in self._send_replies_result(event, "/auto-route", payload):
            yield result

    # ==================== 帮助命令 ====================
    @filter.command("ow-help", aliases=["守望帮助", "oh"])
    async def cmd_help(self, event: AstrMessageEvent):
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
/week-summary /week [战网ID] - 本周总结(较慢)

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
例如: /ow 帮我看一下海盐冰淇淋#5911这周总结

/ow-help /oh - 显示此帮助信息
        """
        yield event.plain_result(help_text.strip())
