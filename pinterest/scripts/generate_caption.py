#!/usr/bin/env python3
"""
generate_caption.py — Generate Pinterest pin caption using DeepSeek API

style guide 的 schema 跟 TikTok/YouTube 流水线一致——同一个 R2 key
（_config/tiktok.json），同样的字段（tone_notes / examples / core_hashtags），
同样的角度池机制（CAPTION_ANGLES / CAPTION_KNOWLEDGE_ANGLES）。Pinterest 不再
有自己专属的风格指南文件。

但 Pinterest 平台本身有一条 TikTok 没有的硬性限制——整条 Pin 文案（正文+
hashtag）不能超过 500 字符，超了 Buffer 会直接拒绝发布。共用同一套 prompt
结构后，模型容易被 TikTok 那种"自由发挥、写多段场景"的指令带偏，写出超长内容，
所以这里加了"生成→检查长度→超了就重试→还超了就智能截断兜底"的保护，不能只靠
prompt 里的字数建议。

product_info 和 style 由 publish.py 从 R2 读出后传进来（同样是 TikTok/YouTube
已经在用的 key：_config/product-info.md 和 _config/tiktok.json），这里不做
任何本地或独立的 R2 读取。
"""

import os
import random
import requests

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

# Pinterest 平台硬性上限——超过这个数 Buffer 会直接拒绝发布（错误信息：
# "Pinterest posts cannot exceed 500 characters"）。这是真实平台限制，不是
# 风格建议，所以代码层面必须保证，不能只靠 prompt 里"建议"模型遵守。
PINTEREST_MAX_CAPTION_CHARS = 500
CAPTION_LENGTH_RETRIES = 2  # 超长时的重试次数，重试也用完了就走兜底裁切

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


def _generate_caption_body(product_info: str, style: dict, include_link: bool,
                            link_url: str, max_body_chars: int) -> str:
    """单次生成（不含hashtag）。max_body_chars 是这次尝试允许的正文字数上限——
    重试时会逐次收紧这个数字，给模型更明确、更强硬的信号。"""
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
their structure, opening style, or length. These examples are from a DIFFERENT platform with a much
higher character limit — Pinterest pin descriptions must be far shorter and written as ONE tight
paragraph, not multiple short lines/beats):
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
- HARD LIMIT: this text must be under {max_body_chars} characters. Pinterest's platform itself
  rejects any pin description over 500 characters total (and a fixed hashtag block gets appended
  after your text), so going over {max_body_chars} characters here will cause the post to fail
  outright. Stop well short of the limit — shorter is always safer than longer.
- Write it as ONE tight paragraph (or two short sentences at most) — NOT multiple short lines or
  separate beats like a script. A Pinterest description reads as a single flowing thought.
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
    return response.json()["choices"][0]["message"]["content"].strip()


def fit_caption_to_limit(body: str, hashtags: str, max_chars: int = PINTEREST_MAX_CAPTION_CHARS) -> str:
    """保证最终发给Pinterest的文案（正文+hashtag）绝对不超过平台硬性上限。
    优先裁正文，hashtag整段保留（hashtag本身短，裁了对发现力没什么意义）。
    尽量在句子边界（. ! ?）截断，找不到合适的句子边界就退而求其次在单词边界
    截断，不会硬切半个单词。"""
    separator = "\n\n"
    full = f"{body}{separator}{hashtags}"
    if len(full) <= max_chars:
        return full

    budget_for_body = max_chars - len(separator) - len(hashtags)
    if budget_for_body <= 0:
        # 极端情况：hashtag本身就快把额度占满了，直接裁hashtag
        return hashtags[:max_chars]

    truncated = body[:budget_for_body]
    sentence_ends = [truncated.rfind(p) for p in (". ", "! ", "? ", ".\n", "!\n", "?\n")]
    best_cut = max(sentence_ends)
    if best_cut > budget_for_body * 0.5:
        truncated = truncated[:best_cut + 1]
    else:
        last_space = truncated.rfind(" ")
        if last_space > 0:
            truncated = truncated[:last_space]
        truncated = truncated.rstrip() + "…"

    return f"{truncated}{separator}{hashtags}"


def generate_caption(product_info: str, style: dict, include_link: bool = False, link_url: str = None) -> str:
    # core_hashtags 跟 TikTok 一样是固定的——全部带上，不是从池子里随机抽。
    hashtags = " ".join(style["core_hashtags"])

    # 留给正文的字数预算 = 总上限 - 分隔符 - hashtag长度，再留一点余量。
    body_budget = PINTEREST_MAX_CAPTION_CHARS - len("\n\n") - len(hashtags) - 20

    last_body = ""
    for attempt in range(1, CAPTION_LENGTH_RETRIES + 2):  # 含首次尝试
        # 每次重试都把允许的正文字数收紧一点，给模型更强硬的信号
        target = max(150, body_budget - (attempt - 1) * 80)
        body = _generate_caption_body(product_info, style, include_link, link_url, max_body_chars=target)
        last_body = body
        full = f"{body}\n\n{hashtags}"
        if len(full) <= PINTEREST_MAX_CAPTION_CHARS:
            return full
        print(f"  ⚠️ 文案过长（{len(full)}字符 > {PINTEREST_MAX_CAPTION_CHARS}），"
              f"第{attempt}次尝试，{'重试...' if attempt <= CAPTION_LENGTH_RETRIES else '改为裁切兜底。'}")

    return fit_caption_to_limit(last_body, hashtags)


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
    print(f"长度: {len(caption)} 字符\n")
    print(caption)
