from nonebot import on_command, get_driver, logger
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from pjsk_utils import pjsk_utils

# 注册指令
pjsk = on_command("pjsk", aliases={"sekai", "说"}, priority=5, block=True)
driver = get_driver()

@driver.on_startup
async def _update():
    """在 Bot 启动时自动更新一次配置"""
    if not pjsk_utils.characters:
        logger.info("正在初始化 PJSK 贴纸配置...")
        await pjsk_utils.update_config()

@pjsk.handle()
async def handle_pjsk(args: Message = CommandArg()):
    arg_list = args.extract_plain_text().strip().split(maxsplit=1)
    
    if not arg_list:
        await pjsk.finish("用法: /pjsk <ID/名称> <文字>\n例如: /pjsk 62 想要呜呼呼！\n使用 /pjsk list 查看详细列表")

    if arg_list[0] == "list":
        await pjsk.finish("角色列表请查看: https://st.ayaka.one/\n目前支持按 ID 或名称模糊匹配。")

    if len(arg_list) < 2:
        await pjsk.finish("请输入文字内容！")

    char_id = arg_list[0]
    text = arg_list[1]

    await pjsk.send("正在生成表情包，请稍候...")
    
    try:
        img_bytes, error = await pjsk_utils.create_sticker(char_id, text)
        
        if error:
            await pjsk.finish(f"生成失败: {error}")
            
        if img_bytes:
            await pjsk.send(MessageSegment.image(img_bytes))
        else:
            await pjsk.finish("生成失败：未知错误")
            
    except Exception as e:
        logger.opt(exception=True).error("PJSK 表情包生成运行出错")
        await pjsk.finish("运行出错，请联系 Bot 主人查看后台日志。")