#!/usr/bin/env python3
import nonebot
from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter

nonebot.init(
    host="127.0.0.1",
    port=8080,
    superusers=["1609123070"],
)

driver = nonebot.get_driver()
driver.register_adapter(ONEBOT_V11Adapter)

nonebot.load_plugin("cf_plugin")
nonebot.load_plugin("menu")

if __name__ == "__main__":
    nonebot.run()

