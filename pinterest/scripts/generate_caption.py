#!/usr/bin/env python3
"""
generate_caption.py — Generate Pinterest pin caption using DeepSeek API
"""

import json
import os
import random
import requests
from pathlib import Path

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

STYLE_GUIDE_PATH = Path(__file__).parent.parent / "style-guides" / "pinterest.json"
PRODUCT_INFO_PATH = Path(__file__).parent.parent / "product-info.md"


def load_style_guide():
    with open(STYLE_GUIDE_PATH) as f:
        return json.load(f)


def load_product_info():
    with open(PRODUCT_INFO_PATH) as f:
        return f.read()


def generate_caption(include_link: bool = False, link_url: str = None) -> str:
    style = load_style_guide()
    product_info = load_product_info()

    hook_type = random.choice(style["common_hooks"])
    hashtag_pool = style["hashtags"]["primary"] + style["hashtags"]["secondary"]
    random.shuffle(hashtag_pool)
    hashtags = " ".join(hashtag_pool[:5])

    link_instruction = ""
    if include_link and link_url:
        link_instruction = f"\n- End the caption naturally, then on a new line add exactly: {link_url}"

    prompt = f"""You write Pinterest pin descriptions for barekey, a MacBook keyboard skin brand.

Product info:
{product_info}

Style guide:
- Tone: {', '.join(style['tone'])}
- Core selling points to draw from: {', '.join(style['core_selling_points'])}
- Use this hook angle for this pin: {hook_type}
- Pinterest format: {style['pinterest_specific']['format']}
- Style notes: {style['pinterest_specific']['style']}
- Avoid: {', '.join(style['pinterest_specific']['avoid'])}

Example captions for reference (do NOT copy, use as tone reference only):
{chr(10).join('- ' + ex for ex in style['example_captions'])}

Write ONE pin description now. Rules:
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

    # Append hashtags
    full_caption = f"{caption}\n\n{hashtags}"
    return full_caption


if __name__ == "__main__":
    import sys
    include_link = "--link" in sys.argv
    link_url = None
    for i, arg in enumerate(sys.argv):
        if arg == "--link-url" and i + 1 < len(sys.argv):
            link_url = sys.argv[i + 1]

    caption = generate_caption(include_link=include_link, link_url=link_url)
    print(caption)
