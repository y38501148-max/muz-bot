
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .compact import compact_request

PLUGIN_NAME = "astrbot_plugin_muz_gateway"
PROMPT_FILE_NAME = "system_prompt.txt"
MAX_CONTEXT_TOKENS = 50_000
COMPACT_TARGET_TOKENS = 45_000


class Main(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.prompt_path = (
            StarTools.get_data_dir(PLUGIN_NAME) / PROMPT_FILE_NAME
        )
        self.prompt_path.touch(exist_ok=True)
        logger.info("系统提示词文件：%s", self.prompt_path)

    def _load_system_prompt(self) -> str:
        try:
            return self.prompt_path.read_text(encoding="utf-8")
        except OSError as error:
            logger.error("读取系统提示词失败：%s", error)
            return ""

    @filter.on_llm_request()
    async def apply_prompt_and_context_guard(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest,
    ) -> None:
        del event
        # This intentionally replaces AstrBot's persona prompt. An empty file
        # therefore means an empty model system prompt, as required.
        request.system_prompt = self._load_system_prompt()
        result = compact_request(
            request.contexts,
            prompt=request.prompt,
            system_prompt=request.system_prompt,
            max_tokens=MAX_CONTEXT_TOKENS,
            target_tokens=COMPACT_TARGET_TOKENS,
        )
        request.contexts = result.contexts
        request.prompt = result.prompt
        request.system_prompt = result.system_prompt
        if result.compacted:
            logger.info(
                "本地 compact 完成，上下文估算值已降至 %s tokens",
                result.estimated_tokens,
            )
