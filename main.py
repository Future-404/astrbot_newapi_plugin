from typing import Optional, Tuple
from functools import wraps
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.api.message_components import At

from .newapi_utils import NewApiCore
from .heist_logic import HeistLogic

def require_binding(f):
    """
    检查命令发起者是否绑定网站ID，若未绑定则中断并提示，若已绑定则附加binding对象以便后续使用。
    """
    @wraps(f)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        user_qq_id = event.get_sender_id()
        
        # 避免重复获取binding
        if hasattr(event, 'binding'):
            async for item in f(self, event, *args, **kwargs):
                yield item
            return

        binding = await self.core.get_user_by_qq(user_qq_id)

        if not binding:
            yield event.plain_result("您尚未绑定网站ID，无法进行此操作。\n请使用 `/绑定 [您的网站ID]` 指令。")
            return
        
        # 附加binding对象到event
        event.binding = binding
        
        async for item in f(self, event, *args, **kwargs):
            yield item
            
    return wrapper

@register(
    "NewAPI_plugin",
    "Future-404",
    "集成了核心用户管理与娱乐功能的New API插件套件。",
    "1.1.0"
)
class NewApiSuitePlugin(Star):
    """
    New API 功能套件主插件类，作为功能套件的唯一入口点。
    """
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.core = NewApiCore(config)
        self.heist_handler = HeistLogic(config, self.core)
        logger.info("[NewAPI Suite] 插件已实例化，准备进行异步初始化...")

    async def initialize(self):
        init_success = await self.core.initialize()
        if init_success:
            logger.info("[NewAPI Suite] 核心服务初始化成功。" )
        else:
            logger.error("[NewAPI Suite] 核心服务初始化失败。" )
            

    
    @filter.command("pingapi")
    async def handle_ping_command(self, event: AstrMessageEvent):
        """响应ping命令，并报告数据库状态。"""
        db_status = "✅ 已连接" if self.core.db_pool is not None else "❌ 连接失败"
        reply = f"""🎉 Pong! NewAPI 插件套件 V1.1.0 正在运行！
--------------------
数据库状态: {db_status}"""
        yield event.plain_result(reply)

    @filter.command("查询余额")
    @require_binding
    async def handle_query_balance(self, event: AstrMessageEvent):
        """允许已绑定用户查询网站余额。"""
        binding = event.binding
        website_user_id = binding['website_user_id']
        api_user_data = await self.core.get_api_user_data(website_user_id)

        if not api_user_data:
            yield event.plain_result("查询失败，无法从网站获取您的余额信息。请稍后再试或联系管理员。" )
            return

        binding_conf = self.config.get('binding_settings', {})
        ratio = binding_conf.get('quota_display_ratio', 500000)
        display_quota = api_user_data.get("quota", 0) / ratio

        reply = f"""查询成功！
--------------------
您绑定的网站ID: {website_user_id}
当前剩余额度: {display_quota:.2f}"""
        
        yield event.plain_result(reply)

    @filter.command("绑定")
    async def handle_bind_command(self, event: AstrMessageEvent, website_user_id: int):
        """处理用户绑定请求，并执行校验。"""
        user_qq_id = event.get_sender_id()

        error_message = (
            await self._check_self_binding(user_qq_id) or
            await self._check_qq_level(event, user_qq_id) or
            await self._check_api_user_exists(website_user_id) or
            await self._check_id_uniqueness(website_user_id)
        )
        
        if error_message:
            yield event.plain_result(error_message)
            return
        
        yield event.plain_result("验证通过，执行绑定...")
        
        success, message = await self._perform_binding_ritual(user_qq_id, website_user_id)
        
        if success:
            await self._send_success_pm(event, user_qq_id, website_user_id)
        
        yield event.plain_result(message)

    @filter.command("签到")
    @require_binding
    async def handle_check_in(self, event: AstrMessageEvent):
        """处理用户每日签到请求。"""
        user_qq_id = event.get_sender_id()
        
        status, details = await self.core.perform_check_in(user_qq_id, binding=event.binding)
        
        check_in_conf = self.config.get('check_in_settings', {})
        
        reply = ""
        match status:
            case "SUCCESS":
                first_bonus_enabled = check_in_conf.get('first_check_in_bonus_enabled', False)
                
                if details["is_first"] and first_bonus_enabled:
                    template = check_in_conf.get('first_check_in_success_template')
                elif details["is_doubled"]:
                    template = check_in_conf.get('check_in_doubled_template')
                else:
                    template = check_in_conf.get('check_in_success_template')
                
                reply = template.format(
                    display_added=f"{details['display_added']:.2f}", 
                    display_total=f"{details['display_total']:.2f}",
                    user_qq=details['user_qq'],
                    site_id=details['site_id']
                )
            case "DISABLED":
                reply = "抱歉，每日签到功能当前未开启。"
            case "ALREADY_CHECKED_IN":
                reply = "您今天已经签过到了，请明天再来吧！"
            case "API_USER_NOT_FOUND":
                reply = "签到失败：无法获取您的网站用户信息，请联系管理员。"
            case "API_UPDATE_FAILED":
                reply = "签到失败：向网站服务器更新额度时发生错误，请稍后再试。"
            case _:
                reply = "签到时发生未知错误，请联系管理员。"
        
        yield event.plain_result(reply)
    @filter.command("解绑")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def handle_unbind_command(self, event: AstrMessageEvent, website_user_id: int):
        """(管理员) 强制解除指定网站ID的绑定。"""
        success, binding_info = await self.core.purge_user_binding(website_user_id)
        
        reply = ""
        if success:
            reply = (
                f"✅ 操作成功！\n"
                f"已将网站ID: {website_user_id}\n"
                f"从QQ用户: {binding_info['qq_id']} 的契约中解放。"
            )
        else:
            if binding_info is None:
                reply = f"❌ 操作无效：未找到网站ID {website_user_id} 的绑定记录。"
            else:
                reply = f"❌ 操作失败：在为网站ID {website_user_id} 执行净化时发生未知错误，请检查后台日志。"
                
        yield event.plain_result(reply)

    @filter.command("查询")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def handle_universal_lookup(self, event: AstrMessageEvent, identifier: int):
        """(管理员) 智能查询，自动识别网站ID或QQ号。"""
        id_type, binding = await self.core.lookup_binding(identifier)
        
        reply = ""
        match id_type:
            case "WEBSITE_ID":
                reply = f"""✅ 查询成功！输入的是【网站ID】
--------------------
网站ID: {binding['website_user_id']}
已绑定至QQ: {binding['qq_id']}
绑定时间: {binding['binding_time'].strftime('%Y-%m-%d %H:%M:%S')}"""
            case "QQ_ID":
                reply = f"""✅ 查询成功！输入的是【QQ号】
--------------------
QQ号: {binding['qq_id']}
已绑定至网站ID: {binding['website_user_id']}
绑定时间: {binding['binding_time'].strftime('%Y-%m-%d %H:%M:%S')}"""
            case "NOT_FOUND":
                reply = f"❌ 查询失败：未在绑定记录中找到与 {identifier} 相关的任何信息。"
        
        yield event.plain_result(reply)

    @filter.command("调整余额")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def handle_adjust_balance(
        self, event: AstrMessageEvent, identifier: int, display_adjustment: float
    ):
        """(管理员) 智能识别ID，并调整用户显示额度。"""
        status, details = await self.core.adjust_balance_by_identifier(
            identifier, display_adjustment
        )
        
        reply = ""
        match status:
            case "SUCCESS":
                action_text = "增加" if display_adjustment >= 0 else "减少"
                reply = f"""✅ 操作成功！
--------------------
目标用户ID: {details['website_user_id']}
已为其{action_text}显示额度: {abs(display_adjustment):.2f}
该用户当前总显示额度为: {details['new_display_quota']:.2f}"""
            case "USER_NOT_FOUND":
                reply = f"❌ 操作失败：未在绑定记录中找到与 {identifier} 相关的用户。"
            case "API_FETCH_FAILED":
                reply = f"❌ 操作失败：无法从网站获取ID为 {details['website_user_id']} 的用户信息。"
            case "API_UPDATE_FAILED":
                reply = f"❌ 操作失败：向网站更新ID为 {details['website_user_id']} 的余额时发生错误。"

        yield event.plain_result(reply)

    @filter.command("打劫")
    async def handle_heist_command(self, event: AstrMessageEvent):
        """(娱乐) 对 @ 的目标发起打劫。"""
        robber_qq_id = event.get_sender_id()

        # 1. 提取目标QQ
        target_qq_ids = [
            seg.qq  # 从At消息段中提取qq号
            for seg in event.get_messages()
            if isinstance(seg, At) and seg.qq != int(event.get_self_id())
        ]

        # 2. 校验
        if not target_qq_ids:
            yield event.plain_result("🤔 打劫谁呢？请 @ 你要打劫的目标。" )
            return
        if len(target_qq_ids) > 1:
            yield event.plain_result("🏃‍♂️ 不要太贪心，一次只能打劫一个目标！" )
            return

        # 3. 获取受害者QQ号
        victim_qq_id = target_qq_ids[0]
        
        status, details = await self.heist_handler.execute_heist(robber_qq_id, victim_qq_id)
        
        # 4. 根据结果生成回复
        heist_conf = self.config.get('heist_settings', {})
        reply = ""

        # --- 缓存模板 ---
        success_template = heist_conf.get('success_template', "成功: +{gain:.2f}")
        critical_template = heist_conf.get('critical_template', "暴击: +{gain:.2f}")
        failure_template = heist_conf.get('failure_template', "失败: -{penalty:.2f}")
        disabled_template = heist_conf.get('disabled_template', "⚔️ 打劫活动尚未开启。" )
        robber_not_bound_template = heist_conf.get('robber_not_bound_template', "🤔 请先绑定账号。" )
        victim_not_found_template = heist_conf.get('victim_not_found_template', "💨 未找到目标 {victim_identifier}。" )
        cannot_rob_self_template = heist_conf.get('cannot_rob_self_template', "🤦‍♂️ 不能打劫自己。" )
        attempts_exceeded_template = heist_conf.get('attempts_exceeded_template', "🥵 次数用尽。" )
        defenses_exceeded_template = heist_conf.get('defenses_exceeded_template', "🛡️ 对方已有防备 (ID:{victim_id})。" )
        cooldown_template = heist_conf.get('cooldown_template', "⏳ 冷却中，剩余 {remaining_time} 秒。")
        # --- 缓存结束 ---

        match status:
            case "SUCCESS":
                reply = success_template.format(gain=details['gain'])
            case "CRITICAL":
                reply = critical_template.format(gain=details['gain'])
            case "FAILURE":
                reply = failure_template.format(penalty=details['penalty'])
            case "DISABLED":
                reply = disabled_template
            case "ROBBER_NOT_BOUND":
                reply = robber_not_bound_template
            case "VICTIM_NOT_FOUND":
                reply = victim_not_found_template.format(victim_identifier=f" @{victim_qq_id}")
            case "CANNOT_ROB_SELF":
                reply = cannot_rob_self_template
            case "ATTEMPTS_EXCEEDED":
                reply = attempts_exceeded_template
            case "DEFENSES_EXCEEDED":
                reply = defenses_exceeded_template.format(victim_id=details['victim_id'])
            case "COOLDOWN_ACTIVE":
                reply = cooldown_template.format(remaining_time=details['remaining_time'])
            case "API_ERROR":
                reply = "- 发生了一个API错误，请联系管理员。"
            case _:
                reply = "❓ 发生未知错误。"
        
        yield event.plain_result(reply)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_group_decrease(self, event: AstrMessageEvent):
        """监听群成员减少事件，执行解绑并发送通知。"""
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        raw = event.message_obj.raw_message
        if not (
            isinstance(raw, dict)
            and raw.get("post_type") == "notice"
            and raw.get("notice_type") == "group_decrease"
        ):
            return
        
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")

        leave_conf = self.config.get('group_leave_settings', {})
        monitored_groups_str = leave_conf.get('group_monitoring_list', [])
        monitored_groups = [int(g) for g in monitored_groups_str if str(g).isdigit()]

        if group_id not in monitored_groups:
            return

        binding = await self.core.get_user_by_qq(user_id)
        if not binding:
            logger.info(f"用户 {user_id} 退出了受监控的群 {group_id}，但其未被绑定，无需净化。" )
            return

        website_user_id = binding['website_user_id']
        success, _ = await self.core.purge_user_binding(website_user_id)

        if success:
            logger.info(f"用户 {user_id} (网站ID: {website_user_id}) 的退群净化仪式成功完成。" )
            
            try:
                sub_type = raw.get("sub_type")
                operator_id = raw.get("operator_id")
                bot = event.bot

                user_info = await bot.get_stranger_info(user_id=user_id, no_cache=True)
                user_nickname = user_info.get("nickname", str(user_id))

                announcement = ""
                if sub_type == "leave":
                    announcement = f"成员【{user_nickname}】({user_id}) 已主动退出群聊。\n其绑定的网站数据已自动解绑，用户组已重置。"
                elif sub_type == "kick":
                    operator_info = await bot.get_group_member_info(group_id=group_id, user_id=operator_id, no_cache=True)
                    operator_nickname = operator_info.get("card") or operator_info.get("nickname", str(operator_id))
                    announcement = f"成员【{user_nickname}】({user_id}) 已被管理员【{operator_nickname}】移出群聊。\n其绑定的网站数据已自动解绑，用户组已重置。"
                
                if announcement:
                    await bot.send_group_msg(group_id=group_id, message=announcement)

            except Exception as e:
                logger.error(f"在为用户 {user_id} 发送退群净化通告时发生错误: {e}", exc_info=True)
        
        event.stop_event()

    # --- 绑定功能辅助方法 ---

    async def _check_self_binding(self, user_qq_id: int) -> Optional[str]:
        """检查用户QQ是否已绑定。"""
        if binding := await self.core.get_user_by_qq(user_qq_id):
            return f"您好，您的QQ已经与网站ID {binding['website_user_id']} 签订了契约，无需重复绑定。"
        return None

    async def _check_qq_level(self, event: AstrMessageEvent, user_qq_id: int) -> Optional[str]:
        binding_conf = self.config.get('binding_settings', {})
        min_level = binding_conf.get('min_qq_level', 16)
        try:
            stranger_info = await event.bot.get_stranger_info(user_id=user_qq_id, no_cache=True)

            raw_level = stranger_info.get('qqLevel') 

            if raw_level is not None:
                user_qq_level = int(raw_level)
                if user_qq_level < min_level:
                    return f"抱歉，您的QQ等级({user_qq_level})未达到所要求的 {min_level} 级，暂时无法绑定。"
            else:
                logger.warning(f"无法从API获取用户 {user_qq_id} 的QQ等级，将跳过此项检查。" )
        except Exception as e:
            logger.warning(f"获取QQ等级失败，跳过检查: {e}", exc_info=True)
        return None

    async def _check_api_user_exists(self, website_user_id: int) -> Optional[str]:
        """检查网站用户ID是否存在。"""
        if not await self.core.get_api_user_data(website_user_id):
            return f"审核失败：网站中不存在ID为 {website_user_id} 的用户，请检查您的ID。"
        return None

    async def _check_id_uniqueness(self, website_user_id: int) -> Optional[str]:
        """检查网站用户ID是否已被他人绑定。"""
        if await self.core.get_user_by_website_id(website_user_id):
            return f"审核失败：ID {website_user_id} 已被另一位用户绑定，无法操作。"
        return None

    async def _perform_binding_ritual(self, user_qq_id: int, website_user_id: int) -> Tuple[bool, str]:
        """
        执行最终的绑定操作，包含数据库写入和API更新，失败时回滚。
        """
        try:
            await self.core.insert_binding(user_qq_id, website_user_id)
            
            api_user_data = await self.core.get_api_user_data(website_user_id)
            binding_conf = self.config.get('binding_settings', {})
            target_group = binding_conf.get('binding_group', 'default')
            
            if api_user_data:
                api_user_data['group'] = target_group
                update_success = await self.core.update_api_user(api_user_data)
                if not update_success:
                    raise Exception("API group update failed.")
            else:
                raise Exception("API user data not found during binding ritual.")

            return True, f"""恭喜您！绑定成功！
您的QQ现已与网站ID {website_user_id} 绑定。
已自动为您晋升至【{target_group}】分组。"""
        
        except Exception as e:
            logger.error(f"绑定仪式中发生错误: {e}", exc_info=True)
            await self.core.delete_binding(qq_id=user_qq_id)
            return False, "绑定过程中发生未知错误，操作已自动撤销，请联系管理员。"

    async def _send_success_pm(self, event: AstrMessageEvent, user_qq_id: int, website_user_id: int):
        """如果配置允许，发送绑定成功私信。"""
        pm_conf = self.config.get('optional_pm_settings', {})
        if not pm_conf.get('enable_bind_success_pm'):
            return
        
        try:
            template = pm_conf.get('bind_success_pm_template', "绑定成功！")
            group = self.config.get('binding_settings.binding_group', 'default')

            user_nickname = str(user_qq_id)
            try:
                stranger_info = await event.bot.get_stranger_info(user_id=user_qq_id, no_cache=True)
                user_nickname = stranger_info.get("nickname", str(user_qq_id))
            except Exception as e:
                logger.warning(f"为私信模板获取QQ昵称失败: {e}", exc_info=True)

            site_username = "未知"
            api_user_data = await self.core.get_api_user_data(website_user_id)
            if api_user_data:
                site_username = api_user_data.get("username", "未知")

            content = template.format(
                id=website_user_id,
                group=group,
                user_qq=user_qq_id,
                user_nickname=user_nickname,
                site_username=site_username
            )
            
            await event.bot.send_private_msg(user_id=user_qq_id, message=content)
            logger.info(f"成功发送绑定成功私信至 {user_qq_id}。" )
        except Exception as e:
            logger.error(f"发送绑定成功私信失败: {e}", exc_info=True)
