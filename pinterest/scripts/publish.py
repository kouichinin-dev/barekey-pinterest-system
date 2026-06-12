#!/usr/bin/env python3
"""
publish.py — Select next image from R2, generate caption, publish to Pinterest via Buffer GraphQL API
每天触发3次，每周21个Pin，位置0带主页链接，位置10带产品页链接
发布完成后发邮件报告到 163 邮箱
"""

import json
import os
import sys
import smtplib
import boto3
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
R2_ENDPOINT   = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY"]
R2_SECRET_KEY = os.environ["R2_SECRET_KEY"]
R2_BUCKET     = os.environ["R2_BUCKET"]
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "https://pub-f38f66bcaab742dfb6263106bb8c6142.r2.dev")

BUFFER_TOKEN      = os.environ["BUFFER_ACCESS_TOKEN"]
BUFFER_CHANNEL_ID = os.environ["BUFFER_PINTEREST_CHANNEL_ID"]

MAIL_USER = os.environ["MAIL_163_USER"]
MAIL_PASS = os.environ["MAIL_163_PASS"]
MAIL_TO   = "19801152287@163.com"

BUFFER_GRAPHQL_URL = "https://api.buffer.com/graphql"
STATE_KEY          = "_system/pinterest-rotation-state.json"
IMAGE_PREFIXES     = [str(i) + "/" for i in range(1, 16)]

HOMEPAGE_URL     = "https://barekey.net"
PRODUCT_PAGE_URL = "https://barekey.net/products/macbook-key-skin"

WEEKLY_CYCLE     = 21
HOMEPAGE_POS     = 0
PRODUCT_PAGE_POS = 10


# ── R2 helpers ────────────────────────────────────────────────────────────────
def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def list_all_images(s3) -> list[str]:
    images = []
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    for prefix in IMAGE_PREFIXES:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if Path(key).suffix.lower() in image_exts:
                    images.append(key)
    images.sort()
    return images


def load_state(s3) -> dict:
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=STATE_KEY)
        return json.loads(obj["Body"].read())
    except Exception:
        return {"index": 0, "total_published": 0, "last_published": None}


def save_state(s3, state: dict):
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=STATE_KEY,
        Body=json.dumps(state, indent=2).encode(),
        ContentType="application/json",
    )


# ── Link schedule ─────────────────────────────────────────────────────────────
def get_link_for_pin(total_published: int) -> tuple[bool, str | None]:
    pos = total_published % WEEKLY_CYCLE
    if pos == HOMEPAGE_POS:
        return True, HOMEPAGE_URL
    elif pos == PRODUCT_PAGE_POS:
        return True, PRODUCT_PAGE_URL
    return False, None


# ── Buffer GraphQL API ────────────────────────────────────────────────────────
def create_pin_via_buffer(image_url: str, caption: str, link_url: str | None) -> str:
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        post {
          id
          status
        }
      }
    }
    """

    variables = {
        "input": {
            "channelId": BUFFER_CHANNEL_ID,
            "content": {
                "text": caption,
                "media": [{"url": image_url, "type": "IMAGE"}],
            },
            "publishingDetails": {
                "publishNow": True,
            },
        }
    }

    if link_url:
        variables["input"]["content"]["link"] = link_url

    headers = {
        "Authorization": f"Bearer {BUFFER_TOKEN}",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        BUFFER_GRAPHQL_URL,
        headers=headers,
        json={"query": mutation, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()

    data = resp.json()
    if "errors" in data:
        print(f"Buffer GraphQL errors: {json.dumps(data['errors'], indent=2)}")
        raise RuntimeError("Buffer API returned errors")

    post_id = data["data"]["createPost"]["post"]["id"]
    return post_id


# ── Email report ──────────────────────────────────────────────────────────────
def send_report(success: bool, post_id: str, image_key: str, image_url: str,
                caption: str, link_url: str | None, cycle_pos: int,
                total_published: int, error_msg: str = ""):

    status_str = "✅ 成功" if success else "❌ 失败"
    subject = f"[barekey Pinterest] {status_str} — Pin #{total_published + 1}"

    if success:
        link_line = f"关联链接: {link_url}" if link_url else "关联链接: 无"
        body = f"""Pinterest Pin 发布报告

状态: {status_str}
时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC
Post ID: {post_id}

图片: {image_key}
图片URL: {image_url}

周期位置: {cycle_pos + 1} / {WEEKLY_CYCLE}
累计发布: {total_published + 1} 个 Pin
{link_line}

文案:
{caption}
"""
    else:
        body = f"""Pinterest Pin 发布失败

时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC
错误信息: {error_msg}

图片: {image_key}
周期位置: {cycle_pos + 1} / {WEEKLY_CYCLE}
"""

    msg = MIMEMultipart()
    msg["From"]    = MAIL_USER
    msg["To"]      = MAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.163.com", 465) as server:
        server.login(MAIL_USER, MAIL_PASS)
        server.sendmail(MAIL_USER, MAIL_TO, msg.as_string())

    print(f"Report email sent to {MAIL_TO}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.utcnow().isoformat()}] Pinterest publish starting")

    s3 = get_s3()

    all_images = list_all_images(s3)
    if not all_images:
        print("ERROR: No images found in R2")
        sys.exit(1)
    print(f"Found {len(all_images)} images across all groups")

    state = load_state(s3)
    current_index  = state["index"] % len(all_images)
    total_published = state["total_published"]
    cycle_pos      = total_published % WEEKLY_CYCLE

    print(f"Cycle position: {cycle_pos + 1}/{WEEKLY_CYCLE}")

    image_key = all_images[current_index]
    image_url = f"{R2_PUBLIC_URL.rstrip('/')}/{image_key}"
    print(f"Selected image: {image_key} (index {current_index}/{len(all_images)})")

    include_link, link_url = get_link_for_pin(total_published)
    if include_link:
        print(f"This pin includes link: {link_url}")
    else:
        print("This pin has no link")

    print("Generating caption...")
    from generate_caption import generate_caption
    caption = generate_caption(include_link=include_link, link_url=link_url)
    print(f"Caption:\n{caption}\n")

    try:
        print("Publishing via Buffer...")
        post_id = create_pin_via_buffer(image_url, caption, link_url)
        print(f"Published post ID: {post_id}")

        state["index"]          = (current_index + 1) % len(all_images)
        state["total_published"] = total_published + 1
        state["last_published"]  = datetime.utcnow().isoformat()
        state["last_post_id"]    = post_id
        state["last_image"]      = image_key
        save_state(s3, state)

        send_report(
            success=True, post_id=post_id, image_key=image_key,
            image_url=image_url, caption=caption, link_url=link_url,
            cycle_pos=cycle_pos, total_published=total_published,
        )
        print(f"Done. Total published: {state['total_published']}")

    except Exception as e:
        error_msg = str(e)
        print(f"ERROR: {error_msg}")
        send_report(
            success=False, post_id="", image_key=image_key,
            image_url=image_url, caption=caption, link_url=link_url,
            cycle_pos=cycle_pos, total_published=total_published,
            error_msg=error_msg,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
