#!/usr/bin/env python3
"""
publish.py — Pinterest 发布主脚本（纯图片版）

从 R2 的 pics/ 文件夹轮转取图片 → 裁切 9:16 → AI生成文案 → 通过 Buffer 发布。
每21个Pin为一个周期，在固定位置插入链接。
"""

import json
import os
import sys
import io
import random
import smtplib
import time
import boto3
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from urllib.parse import quote
from PIL import Image

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
IMAGE_PREFIX       = "pics/"

PRODUCT_INFO_KEY = "_config/product-info.md"
STYLE_GUIDE_KEY  = "_config/tiktok.json"

HOMEPAGE_URL      = "https://barekey.net"
PRODUCT_PAGE_URL  = "https://barekey.net/products/macbook-key-skin"
ETSY_LISTING_URL  = "https://www.etsy.com/uk/listing/4532962134/macbook-modular-keyboard-skin"
ETSY_SHOP_URL     = "https://www.etsy.com/uk/shop/BAREKEY?ref=shop_profile&listing_id=4532962134"

WEEKLY_CYCLE     = 21
HOMEPAGE_POS     = 0
ETSY_LISTING_POS = 5
PRODUCT_PAGE_POS = 10
ETSY_SHOP_POS    = 15

PINTEREST_BOARD_ID = "1117174320005725402"

OUTPUT_WIDTH  = 1080
OUTPUT_HEIGHT = 1920

# ── Daily schedule (randomized post count + timing) ──────────────────────────
# 每天只触发一次 cron，由这个函数决定当天发几条、每条什么时间发。
# Buffer 的 customScheduled 模式负责在指定时间实际投递。

PUBLISH_WINDOW_START_H = 0     # UTC 00:00
PUBLISH_WINDOW_END_H   = 20    # UTC 20:00
MIN_GAP_SECONDS        = 3 * 3600   # 帖子之间最少间隔 3 小时


def calculate_daily_schedule(weekday_range=(2, 3), weekend_range=(3, 4)) -> list:
    """Return a sorted list of due_at ISO 8601 strings for today's posts.

    Picks a random post count based on day-of-week, then distributes
    that many posts randomly across the publishing window with minimum
    spacing.  Each due_at is at least 10 minutes in the future so
    Buffer has time to process the post before the scheduled time.
    """
    now = datetime.utcnow()
    is_weekend = now.weekday() >= 5   # Sat=5, Sun=6

    lo, hi = weekend_range if is_weekend else weekday_range
    num_posts = random.randint(lo, hi)

    window_start = max(
        now + timedelta(minutes=10),
        now.replace(hour=PUBLISH_WINDOW_START_H, minute=0, second=0, microsecond=0),
    )
    window_end = now.replace(hour=PUBLISH_WINDOW_END_H, minute=0, second=0, microsecond=0)
    if window_end <= window_start:
        # Past today's window — push everything to tomorrow
        window_end += timedelta(days=1)
        window_start = max(
            now + timedelta(minutes=10),
            window_end - timedelta(hours=PUBLISH_WINDOW_END_H - PUBLISH_WINDOW_START_H),
        )

    window_seconds = int((window_end - window_start).total_seconds())

    times = []
    for _ in range(num_posts):
        placed = False
        for _attempt in range(200):
            offset = random.randint(0, max(0, window_seconds))
            candidate = window_start + timedelta(seconds=offset)
            if all(abs((candidate - t).total_seconds()) >= MIN_GAP_SECONDS for t in times):
                times.append(candidate)
                placed = True
                break
        if not placed:
            offset = random.randint(0, max(0, window_seconds))
            times.append(window_start + timedelta(seconds=offset))

    times.sort()
    due_ats = [t.strftime("%Y-%m-%dT%H:%M:%S.000Z") for t in times]

    day_label = "Weekend" if is_weekend else "Weekday"
    time_labels = [t.strftime("%H:%M") for t in times]
    print(f"  📅 {day_label} schedule: {num_posts} post(s) at {time_labels} UTC")

    return due_ats


# ── R2 helpers ────────────────────────────────────────────────────────────────
def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def r2_read(s3, key: str) -> str:
    obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
    return obj["Body"].read().decode("utf-8")


def public_url(key: str) -> str:
    return R2_PUBLIC_URL.rstrip("/") + "/" + quote(key, safe="/")


def list_all_images(s3) -> list[str]:
    images = []
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=IMAGE_PREFIX):
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


# ── Image processing ─────────────────────────────────────────────────────────
def crop_to_9_16(s3, image_key: str) -> tuple[str, str]:
    obj = s3.get_object(Bucket=R2_BUCKET, Key=image_key)
    img_data = obj["Body"].read()
    img = Image.open(io.BytesIO(img_data)).convert("RGB")

    src_w, src_h = img.size
    target_ratio = OUTPUT_WIDTH / OUTPUT_HEIGHT

    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    img = img.resize((OUTPUT_WIDTH, OUTPUT_HEIGHT), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)

    tmp_key = f"_pinterest_tmp/{Path(image_key).stem}_9x16.jpg"
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=tmp_key,
        Body=buf.getvalue(),
        ContentType="image/jpeg",
    )

    return tmp_key, public_url(tmp_key)


def delete_tmp(s3, tmp_key: str):
    try:
        s3.delete_object(Bucket=R2_BUCKET, Key=tmp_key)
    except Exception:
        pass


# ── Link schedule ─────────────────────────────────────────────────────────────
def get_link_for_pin(total_published: int) -> tuple[bool, str | None]:
    pos = total_published % WEEKLY_CYCLE
    if pos == HOMEPAGE_POS:
        return True, HOMEPAGE_URL
    elif pos == ETSY_LISTING_POS:
        return True, ETSY_LISTING_URL
    elif pos == PRODUCT_PAGE_POS:
        return True, PRODUCT_PAGE_URL
    elif pos == ETSY_SHOP_POS:
        return True, ETSY_SHOP_URL
    return False, None


# ── Buffer GraphQL API ────────────────────────────────────────────────────────
def create_pin_via_buffer(image_url: str, caption: str, link_url: str | None,
                           title: str = "", due_at: str = None) -> str:
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
            status
          }
        }
        ... on MutationError {
          message
        }
        ... on UnexpectedError {
          message
        }
        __typename
      }
    }
    """

    assets = [{"image": {"url": image_url}}]

    post_input = {
        "channelId": BUFFER_CHANNEL_ID,
        "schedulingType": "automatic",
        "mode": "customScheduled" if due_at else "addToQueue",
        "text": caption,
        "assets": assets,
        "metadata": {
            "pinterest": {
                "boardServiceId": PINTEREST_BOARD_ID,
                "title": title,
                **({"url": link_url} if link_url else {}),
            }
        },
    }
    if due_at:
        post_input["dueAt"] = due_at

    variables = {"input": post_input}

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
        raise RuntimeError("Buffer API errors: " + json.dumps(data["errors"]))

    result = data["data"]["createPost"]
    typename = result.get("__typename", "")
    print(f"Buffer response type: {typename}")

    if typename in ("MutationError", "UnexpectedError"):
        raise RuntimeError(f"Buffer {typename}: {result.get('message', 'unknown error')}")

    post_id = result.get("post", {}).get("id", "unknown")
    return post_id


# ── Email report ──────────────────────────────────────────────────────────────
def send_report(success: bool, post_id: str, media_key: str, media_url: str,
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

图片: {media_key}
图片URL: {media_url}

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

图片: {media_key}
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
def publish_one_pin(s3, rotation_state: dict, all_images: list,
                     product_info: str, style: dict, generate_caption,
                     due_at: str = None) -> bool:
    """Publish a single pin. Returns True on success, False on failure.
    Updates rotation_state in place and saves to R2 on success."""

    current_index   = rotation_state["index"] % len(all_images)
    total_published = rotation_state["total_published"]
    cycle_pos       = total_published % WEEKLY_CYCLE

    print(f"  Cycle position: {cycle_pos + 1}/{WEEKLY_CYCLE}")

    image_key = all_images[current_index]
    print(f"  Selected image: {image_key} (index {current_index}/{len(all_images)})")

    include_link, link_url = get_link_for_pin(total_published)
    if include_link:
        print(f"  This pin includes link: {link_url}")
    else:
        print(f"  This pin has no link")

    print("  Generating caption...")
    caption = generate_caption(product_info, style, include_link=include_link, link_url=link_url)
    print(f"  Caption:\n{caption}\n")

    print("  Cropping image to 9:16...")
    tmp_key, image_url = crop_to_9_16(s3, image_key)
    print(f"  Cropped image URL: {image_url}")

    try:
        schedule_label = f" (scheduled {due_at})" if due_at else ""
        print(f"  Publishing via Buffer...{schedule_label}")
        post_id = create_pin_via_buffer(image_url, caption, link_url, due_at=due_at)
        print(f"  Published post ID: {post_id}")

        rotation_state["index"]           = (current_index + 1) % len(all_images)
        rotation_state["total_published"] = total_published + 1
        rotation_state["last_published"]  = datetime.utcnow().isoformat()
        rotation_state["last_post_id"]    = post_id
        rotation_state["last_image"]      = image_key
        save_state(s3, rotation_state)

        send_report(
            success=True, post_id=post_id, media_key=image_key,
            media_url=image_url, caption=caption, link_url=link_url,
            cycle_pos=cycle_pos, total_published=total_published,
        )
        print(f"  Done. Total published: {rotation_state['total_published']}")
        return True

    except Exception as e:
        error_msg = str(e)
        print(f"  ERROR: {error_msg}")
        send_report(
            success=False, post_id="", media_key=image_key,
            media_url=image_url, caption=caption, link_url=link_url,
            cycle_pos=cycle_pos, total_published=total_published,
            error_msg=error_msg,
        )
        return False


def main():
    print(f"[{datetime.utcnow().isoformat()}] Pinterest publish starting")

    s3 = get_s3()

    rotation_state = load_state(s3)
    product_info = r2_read(s3, PRODUCT_INFO_KEY)
    style        = json.loads(r2_read(s3, STYLE_GUIDE_KEY))

    from generate_caption import generate_caption

    all_images = list_all_images(s3)
    if not all_images:
        print("ERROR: No images found in R2 pics/")
        sys.exit(1)
    print(f"Found {len(all_images)} images in {IMAGE_PREFIX}")

    schedule = calculate_daily_schedule()

    failures = []
    for i, due_at in enumerate(schedule):
        print(f"\n📦 Pin {i+1}/{len(schedule)} — scheduled for {due_at}")
        ok = publish_one_pin(s3, rotation_state, all_images, product_info,
                              style, generate_caption, due_at=due_at)
        if not ok:
            failures.append(i + 1)

    if failures:
        print(f"\n❌ {len(failures)}/{len(schedule)} pin(s) failed: #{', #'.join(str(f) for f in failures)}")
        sys.exit(1)

    print(f"\n✅ All {len(schedule)} pin(s) scheduled successfully.")


if __name__ == "__main__":
    main()
