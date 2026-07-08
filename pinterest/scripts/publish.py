#!/usr/bin/env python3
"""
publish.py — Pinterest 发布主脚本

发布逻辑（原创优先，自动生成兜底，与 TikTok/YouTube 的 _manual/ 体系共用同一套
状态文件 _manual/state.json，新增 pinterest_published / pinterest_published_at
字段）：

1. 检查 _manual/videos/ 是否有未发布到 Pinterest 的原创视频（按文件名顺序找最早
   一个 state 里 pinterest_published 不为 true 的素材）。
   (a) 没有 → 走原有的全库顺序轮转裁图 + AI文案自动发布流程。
   (b) 有 → 检查 _manual/captions/{asset_id}.txt（与 TikTok/YouTube 共用同一份
       文案文件）：
       - 没有文案 → 调用 generate_caption() 自动生成。
       - 有文案 → 复用该文案（TITLE 进 Pinterest 的 title 字段，DESCRIPTION 作为
         正文）。

人工发布的 Pin 同样参与现有「21个一周期，位置0/10插链接」的规则——
total_published 计数器由人工和自动两条路径共同递增，但只有自动路径会移动图片
轮转指针 index。

图片自动裁切为 9:16 竖版；原创视频走 faststart remux 后直接以视频 Pin 发布，不
做画幅裁切。
"""

import json
import os
import sys
import io
import smtplib
import subprocess
import tempfile
import time
import boto3
import requests
from datetime import datetime
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
IMAGE_PREFIXES     = [str(i) + "/" for i in range(1, 16)]

# Config files：现在跟 TikTok/YouTube 读的是完全同一个 R2 key——product_info
# 内容本来就一样，style guide 的 schema 也已经改成跟 TikTok 一致
# （tone_notes/examples/core_hashtags），所以不再需要 Pinterest 专属的配置文件，
# 也不需要任何额外的上传迁移。
PRODUCT_INFO_KEY = "_config/product-info.md"
STYLE_GUIDE_KEY  = "_config/tiktok.json"

# Shared manual-asset library — same R2 paths the TikTok/YouTube/Snapchat
# pipeline reads/writes. _manual/state.json already tracks "published"
# (TikTok), "youtube_published" (YouTube), "snapchat_emailed" (Snapchat) as
# independent fields per asset_id record; this adds "pinterest_published" /
# "pinterest_published_at" to that same shared record.
MANUAL_VIDEOS_PREFIX   = "_manual/videos/"
MANUAL_CAPTIONS_PREFIX = "_manual/captions/"
MANUAL_STATE_KEY        = "_manual/state.json"
MANUAL_PROCESSED_PREFIX = "_manual/processed/"

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

# 输出尺寸 9:16（仅用于自动生成流程的图片裁切，原创视频不做画幅裁切）
OUTPUT_WIDTH  = 1080
OUTPUT_HEIGHT = 1920

MIN_VALID_VIDEO_BYTES = 50_000  # 50KB —比这个还小基本就是空文件/坏文件
MIN_VALID_IMAGE_BYTES = 5_000    # 5KB —封面图比这个还小基本是坏文件
VIDEO_URL_CHECK_RETRIES = 6
VIDEO_URL_CHECK_DELAY_SECONDS = 5


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
    """Pinterest 专属的轮转/计数状态（_system/pinterest-rotation-state.json）。
    index 只在自动化流程里前进；total_published 由自动+人工两条路径共同递增，
    因为它同时是「带链接周期」的唯一依据。"""
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


# ── Image processing（仅自动生成流程使用）────────────────────────────────────
def crop_to_9_16(s3, image_key: str) -> tuple[str, str]:
    """
    下载原图，居中裁切为 9:16，上传到 R2 _pinterest_tmp/，返回 (tmp_key, public_url)
    """
    obj = s3.get_object(Bucket=R2_BUCKET, Key=image_key)
    img_data = obj["Body"].read()
    img = Image.open(io.BytesIO(img_data)).convert("RGB")

    src_w, src_h = img.size
    target_ratio = OUTPUT_WIDTH / OUTPUT_HEIGHT  # 9/16

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


# ── Manual asset helpers（与 TikTok/YouTube 共用同一套 R2 路径）───────────────
def load_manual_state(s3) -> dict:
    try:
        return json.loads(r2_read(s3, MANUAL_STATE_KEY))
    except Exception:
        return {}


def save_manual_state(s3, state: dict):
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=MANUAL_STATE_KEY,
        Body=json.dumps(state, indent=2).encode(),
        ContentType="application/json",
    )


def list_manual_video_ids(s3) -> list[str]:
    """按文件名（去掉后缀）排序返回 _manual/videos/ 下所有素材的 asset_id。"""
    paginator = s3.get_paginator("list_objects_v2")
    ids = []
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=MANUAL_VIDEOS_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith((".mp4", ".mov", ".m4v")):
                filename = key[len(MANUAL_VIDEOS_PREFIX):]
                asset_id, _ext = os.path.splitext(filename)
                if asset_id:
                    ids.append(asset_id)
    return sorted(ids)


def find_unpublished_manual_asset_pinterest(s3) -> str | None:
    """返回最早一个还没发过 Pinterest 的原创素材 asset_id；没有则返回 None。
    用的是 "pinterest_published" 字段——与 TikTok 的 "published"、YouTube 的
    "youtube_published"、Snapchat 的 "snapchat_emailed" 是同一条 state.json
    记录里互相独立的字段，已经发过其他平台但没发过 Pinterest 的素材，照样会
    被这里捡到。"""
    state = load_manual_state(s3)
    for asset_id in list_manual_video_ids(s3):
        if not state.get(asset_id, {}).get("pinterest_published", False):
            return asset_id
    return None


def find_manual_video_key(s3, asset_id: str) -> str:
    for ext in (".mp4", ".mov", ".m4v"):
        key = f"{MANUAL_VIDEOS_PREFIX}{asset_id}{ext}"
        try:
            s3.head_object(Bucket=R2_BUCKET, Key=key)
            return key
        except Exception:
            continue
    raise RuntimeError(f"No video file found for manual asset_id={asset_id}")


def normalize_video_for_publish(local_input_path: str, tmpdir: str) -> str:
    """faststart remux（moov atom 前移），让 Buffer 能正确读出视频时长/元数据。
    先尝试无损 remux，失败则回退到完整转码。逻辑与 TikTok/YouTube 流程完全一致。
    """
    output_path = os.path.join(tmpdir, "normalized.mp4")

    remux_cmd = [
        "ffmpeg", "-y",
        "-i", local_input_path,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    print("  正在做 faststart remux...")
    result = subprocess.run(remux_cmd, capture_output=True, text=True)

    remux_ok = (
        result.returncode == 0
        and os.path.exists(output_path)
        and os.path.getsize(output_path) >= MIN_VALID_VIDEO_BYTES
    )

    if not remux_ok:
        print("  无损 remux 失败或产物无效，回退到完整转码...")
        print(result.stderr[-1500:])
        reencode_cmd = [
            "ffmpeg", "-y",
            "-i", local_input_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]
        result2 = subprocess.run(reencode_cmd, capture_output=True, text=True)
        if result2.returncode != 0 or not os.path.exists(output_path):
            print(result2.stderr[-1500:])
            raise RuntimeError("ffmpeg 处理原创视频失败（remux 和转码都失败了）")

    print(f"  视频处理完成: {os.path.getsize(output_path)} bytes")
    return output_path


def upload_normalized_manual_video(s3, local_path: str, asset_id: str) -> tuple[str, str]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    key = f"{MANUAL_PROCESSED_PREFIX}{asset_id}-pinterest-{timestamp}.mp4"
    s3.upload_file(local_path, R2_BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
    url = public_url(key)
    print(f"  已上传处理后的视频: {url}")
    return url, key


def verify_url_ready(url: str, min_bytes: int = MIN_VALID_VIDEO_BYTES, retries: int = VIDEO_URL_CHECK_RETRIES) -> int:
    """轮询公开URL，确认 R2 CDN 已生效且文件大小正常。视频和封面图都用这个。"""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.head(url, timeout=15, allow_redirects=True)
            content_length = int(resp.headers.get("Content-Length", 0))
            if resp.status_code == 200 and content_length >= min_bytes:
                print(f"  URL已就绪: {content_length} bytes (第{attempt}次检查) — {url}")
                return content_length
            last_error = f"status={resp.status_code}, content_length={content_length}"
        except Exception as e:
            last_error = str(e)

        print(f"  URL还没就绪 (第{attempt}/{retries}次): {last_error}")
        if attempt < retries:
            time.sleep(VIDEO_URL_CHECK_DELAY_SECONDS)

    raise RuntimeError(f"URL在{retries}次检查后仍未就绪: {url} ({last_error})")


def verify_video_url_ready(video_url: str) -> int:
    return verify_url_ready(video_url, min_bytes=MIN_VALID_VIDEO_BYTES)


def extract_video_thumbnail(video_path: str, tmpdir: str, seek_seconds: float = 0.5) -> str:
    """从视频里截一帧当 Pinterest 视频Pin的封面图。Buffer 的 Pinterest 接口
    报错明确要求视频Pin也必须带至少一张image asset——不会像Buffer网页版那样
    自动帮你截帧，所以这一步得自己做。

    默认从第0.5秒截（避开很多视频开头的黑场/淡入），如果视频比这还短就退到
    第0秒重试一次。
    """
    output_path = os.path.join(tmpdir, "thumbnail.jpg")

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek_seconds),
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        output_path,
    ]
    print(f"  正在从第{seek_seconds}秒截取封面图...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    ok = result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) >= MIN_VALID_IMAGE_BYTES
    if not ok:
        print("  截帧失败或视频太短，退到第0秒重试...")
        cmd0 = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "2",
            output_path,
        ]
        result0 = subprocess.run(cmd0, capture_output=True, text=True)
        if result0.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) < MIN_VALID_IMAGE_BYTES:
            print(result0.stderr[-1500:])
            raise RuntimeError("ffmpeg 截取视频封面失败")

    print(f"  封面图截取完成: {os.path.getsize(output_path)} bytes")
    return output_path


def upload_thumbnail(s3, local_path: str, asset_id: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    key = f"{MANUAL_PROCESSED_PREFIX}{asset_id}-pinterest-thumb-{timestamp}.jpg"
    s3.upload_file(local_path, R2_BUCKET, key, ExtraArgs={"ContentType": "image/jpeg"})
    url = public_url(key)
    print(f"  已上传封面图: {url}")
    return url


def parse_manual_caption(raw_text: str) -> dict:
    """解析 TITLE: / DESCRIPTION: 格式的文案文件——与 TikTok/YouTube 共用同一份
    _manual/captions/{asset_id}.txt，解析规则必须保持一致。"""
    title_marker = "TITLE:"
    desc_marker  = "DESCRIPTION:"

    title_idx = raw_text.find(title_marker)
    desc_idx  = raw_text.find(desc_marker)

    if title_idx == -1 or desc_idx == -1:
        raise RuntimeError("原创文案文件必须同时包含 'TITLE:' 和 'DESCRIPTION:' 标签。")

    title_text = raw_text[title_idx + len(title_marker):desc_idx].strip()
    desc_text  = raw_text[desc_idx + len(desc_marker):].strip()

    if not title_text:
        raise RuntimeError("原创文案文件的 TITLE 部分是空的。")
    if not desc_text:
        raise RuntimeError("原创文案文件的 DESCRIPTION 部分是空的。")

    return {"title": title_text, "description": desc_text}


def manual_caption_exists(s3, asset_id: str) -> bool:
    key = f"{MANUAL_CAPTIONS_PREFIX}{asset_id}.txt"
    try:
        s3.head_object(Bucket=R2_BUCKET, Key=key)
        return True
    except Exception:
        return False


def load_manual_caption_text(s3, asset_id: str) -> dict:
    key = f"{MANUAL_CAPTIONS_PREFIX}{asset_id}.txt"
    raw_text = r2_read(s3, key)
    print(f"  使用已有文案: {key}")
    return parse_manual_caption(raw_text)


def mark_manual_asset_published_pinterest(s3, asset_id: str):
    """写入 "pinterest_published" 字段——与 TikTok 的 "published"、YouTube 的
    "youtube_published"、Snapchat 的 "snapchat_emailed" 是同一条记录里互相
    独立的字段，不会动到其他平台的状态。"""
    state = load_manual_state(s3)
    if asset_id not in state:
        state[asset_id] = {}
    state[asset_id]["pinterest_published"] = True
    state[asset_id]["pinterest_published_at"] = datetime.utcnow().isoformat()
    save_manual_state(s3, state)
    print(f"  已标记该素材为 Pinterest 已发布: {asset_id}")


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
def create_pin_via_buffer(media_url: str, caption: str, link_url: str | None,
                           is_video: bool = False, title: str = "",
                           thumbnail_url: str | None = None) -> str:
    """通过 Buffer 的 createPost 发布 Pin。is_video=True 时发视频 Pin（原创素材
    走这条），否则发图片 Pin（自动生成流程走这条，行为与之前完全一致）。

    Pinterest 的视频Pin必须额外带一张封面图（Buffer 报错: "Pinterest posts
    require at least one image"），所以 is_video=True 时 assets 数组里同时
    放 image（thumbnail_url）和 video 两个 entry，thumbnailUrl 也顺手写进
    video 对象里做双重保险。
    """
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

    if is_video:
        video_block = {"url": media_url}
        if thumbnail_url:
            video_block["thumbnailUrl"] = thumbnail_url
        assets = []
        if thumbnail_url:
            assets.append({"image": {"url": thumbnail_url}})
        assets.append({"video": video_block})
    else:
        assets = [{"image": {"url": media_url}}]

    variables = {
        "input": {
            "channelId": BUFFER_CHANNEL_ID,
            "schedulingType": "automatic",
            "mode": "addToQueue",
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
    }

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
                total_published: int, error_msg: str = "", is_video: bool = False,
                source: str = "自动生成"):

    media_label = "视频" if is_video else "图片"
    status_str = "✅ 成功" if success else "❌ 失败"
    subject = f"[barekey Pinterest] {status_str} — {source} Pin #{total_published + 1}"

    if success:
        link_line = f"关联链接: {link_url}" if link_url else "关联链接: 无"
        body = f"""Pinterest Pin 发布报告

状态: {status_str}
来源: {source}
时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC
Post ID: {post_id}

{media_label}: {media_key}
{media_label}URL: {media_url}

周期位置: {cycle_pos + 1} / {WEEKLY_CYCLE}
累计发布: {total_published + 1} 个 Pin
{link_line}

文案:
{caption}
"""
    else:
        body = f"""Pinterest Pin 发布失败

来源: {source}
时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC
错误信息: {error_msg}

{media_label}: {media_key}
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


# ── 原创素材发布流程 ────────────────────────────────────────────────────────────
def run_manual_publish_pinterest(s3, asset_id: str, product_info: str, style: dict,
                                  rotation_state: dict):
    """发布一个原创视频素材到 Pinterest。

    文案：复用 _manual/captions/{asset_id}.txt（与 TikTok/YouTube 共用同一份），
    没有就用 generate_caption() 现场生成。链接周期沿用 rotation_state 里的
    total_published（与自动生成流程共享同一个计数器），成功后递增；但不移动
    自动生成流程专用的 index 指针。
    """
    from generate_caption import generate_caption, fit_caption_to_limit

    total_published = rotation_state["total_published"]
    cycle_pos        = total_published % WEEKLY_CYCLE
    include_link, link_url = get_link_for_pin(total_published)

    raw_video_key = ""
    video_url = ""
    thumbnail_url = ""
    try:
        raw_video_key = find_manual_video_key(s3, asset_id)
        print(f"  原创视频（原始文件）: {raw_video_key}")

        raw_bytes = s3.get_object(Bucket=R2_BUCKET, Key=raw_video_key)["Body"].read()

        with tempfile.TemporaryDirectory() as tmpdir:
            raw_ext = Path(raw_video_key).suffix or ".mp4"
            raw_local_path = os.path.join(tmpdir, f"raw{raw_ext}")
            with open(raw_local_path, "wb") as f:
                f.write(raw_bytes)

            normalized_local_path = normalize_video_for_publish(raw_local_path, tmpdir)
            video_url, _processed_key = upload_normalized_manual_video(s3, normalized_local_path, asset_id)

            # Pinterest 的视频Pin必须额外带一张封面图（见 create_pin_via_buffer
            # 的注释），从处理好的视频里截一帧当封面，跟视频一起上传。
            thumbnail_local_path = extract_video_thumbnail(normalized_local_path, tmpdir)
            thumbnail_url = upload_thumbnail(s3, thumbnail_local_path, asset_id)

        print(f"  处理后的视频: {video_url}")
        print(f"  封面图: {thumbnail_url}")
        verify_url_ready(video_url, min_bytes=MIN_VALID_VIDEO_BYTES)
        verify_url_ready(thumbnail_url, min_bytes=MIN_VALID_IMAGE_BYTES)

        # core_hashtags 跟 TikTok 一样是固定的——全部带上，不是从池子里随机抽。
        hashtags = " ".join(style["core_hashtags"])

        if manual_caption_exists(s3, asset_id):
            caption_data = load_manual_caption_text(s3, asset_id)
            pin_title = caption_data["title"]
            body = caption_data["description"]
            if include_link and link_url:
                body += f"\n\n{link_url}"
            caption = fit_caption_to_limit(body, hashtags)
            if len(caption) != len(f"{body}\n\n{hashtags}"):
                print(f"  ⚠️ 原创文案+hashtag超过500字符上限，已自动截断到 {len(caption)} 字符。"
                      f"建议手动精简一下 _manual/captions/{asset_id}.txt 里的文案。")
        else:
            print("  没有找到原创文案，调用AI生成...")
            pin_title = ""
            caption = generate_caption(product_info, style, include_link=include_link, link_url=link_url)
            print("  AI文案生成完成。")

        print(f"  Pin标题: {pin_title or '(空)'}")
        print(f"  文案:\n{caption}\n")

        print("发布原创视频 Pin via Buffer...")
        post_id = create_pin_via_buffer(video_url, caption, link_url, is_video=True,
                                         title=pin_title, thumbnail_url=thumbnail_url)
        print(f"已发布，post ID: {post_id}")

        rotation_state["total_published"] = total_published + 1
        rotation_state["last_published"]  = datetime.utcnow().isoformat()
        rotation_state["last_post_id"]    = post_id
        rotation_state["last_manual_asset_id"] = asset_id
        save_state(s3, rotation_state)

        send_report(
            success=True, post_id=post_id, media_key=raw_video_key,
            media_url=video_url, caption=caption, link_url=link_url,
            cycle_pos=cycle_pos, total_published=total_published,
            is_video=True, source="原创素材",
        )

        # 只有发布+计数都成功才标记，失败的话下次还会重试同一个素材
        mark_manual_asset_published_pinterest(s3, asset_id)
        print(f"完成。累计发布: {rotation_state['total_published']}")

    except Exception as e:
        error_msg = str(e)
        print(f"ERROR: {error_msg}")
        send_report(
            success=False, post_id="", media_key=raw_video_key,
            media_url=video_url, caption="", link_url=link_url,
            cycle_pos=cycle_pos, total_published=total_published,
            error_msg=error_msg, is_video=True, source="原创素材",
        )
        sys.exit(1)


# ── 自动生成发布流程（原有逻辑，未改动行为）──────────────────────────────────
def run_automated_pinterest_flow(s3, product_info: str, style: dict, rotation_state: dict):
    from generate_caption import generate_caption

    all_images = list_all_images(s3)
    if not all_images:
        print("ERROR: No images found in R2")
        sys.exit(1)
    print(f"Found {len(all_images)} images across all groups")

    current_index   = rotation_state["index"] % len(all_images)
    total_published = rotation_state["total_published"]
    cycle_pos       = total_published % WEEKLY_CYCLE

    print(f"Cycle position: {cycle_pos + 1}/{WEEKLY_CYCLE}")

    image_key = all_images[current_index]
    print(f"Selected image: {image_key} (index {current_index}/{len(all_images)})")

    include_link, link_url = get_link_for_pin(total_published)
    if include_link:
        print(f"This pin includes link: {link_url}")
    else:
        print("This pin has no link")

    print("Generating caption...")
    caption = generate_caption(product_info, style, include_link=include_link, link_url=link_url)
    print(f"Caption:\n{caption}\n")

    print("Cropping image to 9:16...")
    tmp_key, image_url = crop_to_9_16(s3, image_key)
    print(f"Cropped image URL: {image_url}")

    try:
        print("Publishing via Buffer...")
        post_id = create_pin_via_buffer(image_url, caption, link_url)
        print(f"Published post ID: {post_id}")

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
        print(f"Done. Total published: {rotation_state['total_published']}")

    except Exception as e:
        error_msg = str(e)
        print(f"ERROR: {error_msg}")
        send_report(
            success=False, post_id="", media_key=image_key,
            media_url=image_url, caption=caption, link_url=link_url,
            cycle_pos=cycle_pos, total_published=total_published,
            error_msg=error_msg,
        )
        sys.exit(1)
    finally:
        delete_tmp(s3, tmp_key)
        print(f"Cleaned up temp file: {tmp_key}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.utcnow().isoformat()}] Pinterest publish starting")

    s3 = get_s3()

    # ── 诊断 ──────────────────────────────────────────────────────────────
    # 排查 NoSuchKey 用：确认实际连的是哪个 bucket/endpoint，以及 _config/
    # 目录下真实存在哪些文件。问题解决后这段可以删掉。
    print(f"[diag] R2_BUCKET   = {R2_BUCKET!r}")
    print(f"[diag] R2_ENDPOINT = {R2_ENDPOINT!r}")
    try:
        resp = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix="_config/")
        keys = [obj["Key"] for obj in resp.get("Contents", [])]
        print(f"[diag] _config/ 下实际存在的文件: {keys}")
    except Exception as e:
        print(f"[diag] 列出 _config/ 失败: {e}")
    # ── 诊断结束 ──────────────────────────────────────────────────────────

    rotation_state = load_state(s3)

    product_info = r2_read(s3, PRODUCT_INFO_KEY)
    style        = json.loads(r2_read(s3, STYLE_GUIDE_KEY))

    manual_asset_id = find_unpublished_manual_asset_pinterest(s3)

    if manual_asset_id:
        print(f"找到未发布的原创素材: {manual_asset_id} —— 使用原创视频。")
        run_manual_publish_pinterest(s3, manual_asset_id, product_info, style, rotation_state)
        print("✅ 完成。")
        return

    print("没有找到未发布的原创素材 —— 走自动生成发布流程。")
    run_automated_pinterest_flow(s3, product_info, style, rotation_state)


if __name__ == "__main__":
    main()
