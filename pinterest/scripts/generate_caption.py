#!/usr/bin/env python3
"""
generate_caption.py — Generate Pinterest pin caption using DeepSeek API

style guide 的 schema 现在完全跟 TikTok/YouTube 流水线一致——同一个 R2 key
（_config/tiktok.json），同样的字段（tone_notes / examples / core_hashtags），
同样的角度池机制（CAPTION_ANGLES / CAPTION_KNOWLEDGE_ANGLES）。Pinterest 不再
有自己专属的风格指南文件。

product_info 和 style 由 publish.py 从 R2 读出后传进来（同样是 TikTok/YouTube
已经在用的 key：_config/product-info.md 和 _config/tiktok.json），这里不做
任何本地或独立的 R2 读取。
"""

import os
import random
import requests

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

# 跟 TikTok/YouTube 流水线一样的角度池机制——每次显式指定一个具体角度，而不是
# 让模型自己"随便发挥"，这才是真正打破文案雷同感的关键。
CAPTION_ANGLES = [
    "Open with a relatable problem or annoyance, then introduce the product as the fix.",
    "Open with a bold, punchy claim or stat about the product — no setup, straight to the hook.",
    "Open with a question that calls out the viewer directly (e.g. 'still doing X?').",
    "Open with a short personal/POV story moment, like you're mid-conversation with a friend.",
    "Open with a contrarian or surprising take that challenges a common assumption.",
    "Open by describing a specific everyday moment/scene where the product comes in clutch.",
    "Open with humor or a playful exaggeration before getting to the product.",
    "Open with a direct comparison ('before vs after' or 'this vs that').",
]

# 知识型角度——分享一个真实的MacBook键盘/屏幕保养小知识（不是barekey专属的事实），
# 再自然过渡到产品。这类角度本身几乎没有事实风险，因为它不是对barekey的产品声明，
# 只有过渡之后的产品部分仍需严格基于product_info。
CAPTION_KNOWLEDGE_ANGLES = [
    "Share a genuine, useful MacBook keyboard/screen care tip (e.g. oil buildup, key imprint marks "
    "on the display, cleaning habits, dust in switches, typing wear patterns) as general knowledge — "
    "NOT a claim about barekey. Spend real time on the tip itself, make it feel like a genuinely useful "
    "share, then transition naturally into how the product fits into solving that problem.",
    "Open by busting a common myth or misconception about protecting a MacBook keyboard (e.g. assuming "
    "a thick keyboard cover is automatically better protection) as general knowledge, then pivot into the "
    "product as the better alternative.",
    "Open with a quick 'did you know' style fact about why MacBook keys/screens get grease marks or wear "
    "over time as general knowledge, then connect it to the product.",
]

ALL_CAPTION_ANGLES = CAPTION_ANGLES + CAPTION_KNOWLEDGE_ANGLES


def generate_caption(product_info: str, style: dict, include_link: bool = False, link_url: str = None) -> str:
    examples_text = "\n\n".join(
        f'TITLE: {ex["title"]}\nDESCRIPTION: {ex["description"]}'
        for ex in style["examples"]
    )
    angle = random.choice(ALL_CAPTION_ANGLES)
    is_knowledge_angle = angle in CAPTION_KNOWLEDGE_ANGLES

    knowledge_note = ""
    if is_knowledge_angle:
        knowledge_note = """
## Note on this angle:
This is a "knowledge-led" post. The opening tip/fact must be genuinely true general knowledge about
MacBooks/keyboards (not specific to barekey) — balance it roughly evenly with the product portion that
follows, rather than rushing through it. The transition into the product should feel natural, like "this
is exactly the kind of problem this solves" rather than an abrupt ad pivot. The product portion still
follows the same strict factual rules below — only claim what's in Product Info.
"""

    link_instruction = ""
    if include_link and link_url:
        link_instruction = f"\n- End the caption naturally, then on a new line add exactly: {link_url}"

    prompt = f"""You write Pinterest pin descriptions for barekey, a MacBook keyboard skin brand.

## Product Info (this is the ONLY source of truth about the product)
{product_info}

## Tone & Style Notes
{style['tone_notes']}

## Real examples from the brand's other content (study the voice and energy only — do NOT copy
their structure, opening style, or length. These examples are NOT Pinterest-format, so also do
NOT copy how they place hashtags):
{examples_text}

## Required angle for THIS pin (do not reuse the angle from the examples above):
{angle}
{knowledge_note}
## STRICT FACTUAL RULES — read carefully, these override the creative instructions above:
1. Every factual claim about the PRODUCT (materials, features, fit, durability, what it does,
   how it works, measurements, prices, guarantees) MUST come directly from the Product Info
   section above. Do not invent, assume, or add any product fact that isn't explicitly stated there.
2. General, true, publicly-known facts about MacBooks or keyboards in general (not specific to
   barekey) are fine to include even though they're not in Product Info.
3. If you want a vivid scene, comparison, joke, or relatable moment to make the angle work, you
   may invent surrounding context — but never invent a NEW product attribute, spec, material,
   capability, or claim while doing so.
4. When in doubt about whether something counts as a barekey-specific "fact" versus general knowledge
   or harmless color/flavor text, leave it out rather than risk adding an unverified product claim.
5. HIGH-RISK CLAIM CATEGORIES — be extremely conservative here: liquid/spill/water resistance,
   allergy/skin-safety claims, heat resistance, impact/drop protection, and long-term durability
   guarantees. NEVER state or imply the product handles any of these UNLESS Product Info explicitly
   says so in those exact terms.

Write ONE Pinterest pin description now. Rules:
- 150 to 300 characters for the main text (can be slightly longer for detail pins)
- Mostly lowercase
- 1-3 emoji max, placed naturally
- Do NOT use markdown formatting
- Do NOT include hashtags (they will be added separately)
- Sound like a real person, not a brand{link_instruction}
- Output ONLY the caption text, nothing else"""

    response = requests.post(
        DEEPSEEK_API_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
            "temperature": 0.9,
        },
        timeout=30,
    )
    response.raise_for_status()
    caption = response.json()["choices"][0]["message"]["content"].strip()

    # core_hashtags 跟 TikTok 一样是固定的——每次全部带上，不是从池子里随机抽。
    hashtags = " ".join(style["core_hashtags"])
    full_caption = f"{caption}\n\n{hashtags}"
    return full_caption


if __name__ == "__main__":
    # 本地单独测试用——正式运行时这个脚本永远是被 publish.py import 调用，
    # product_info/style 由 publish.py 从 R2 读出后直接传参进来。
    import sys
    import boto3

    def _test_r2_read(key: str) -> str:
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY"],
            aws_secret_access_key=os.environ["R2_SECRET_KEY"],
            region_name="auto",
        )
        obj = s3.get_object(Bucket=os.environ["R2_BUCKET"], Key=key)
        return obj["Body"].read().decode("utf-8")

    import json as _json

    include_link = "--link" in sys.argv
    link_url = None
    for i, arg in enumerate(sys.argv):
        if arg == "--link-url" and i + 1 < len(sys.argv):
            link_url = sys.argv[i + 1]

    test_product_info = _test_r2_read("_config/product-info.md")
    test_style        = _json.loads(_test_r2_read("_config/tiktok.json"))

    caption = generate_caption(test_product_info, test_style, include_link=include_link, link_url=link_url)
    print(caption)
