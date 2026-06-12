# barekey-pinterest-system

Pinterest 自动发布流水线 for barekey.net（通过 Buffer GraphQL API 发布）

## 结构
```
pinterest/
├── scripts/
│   ├── publish.py           # 主脚本：选图 → 生成文案 → 经 Buffer 发 Pin
│   └── generate_caption.py  # DeepSeek 生成文案
├── style-guides/
│   └── pinterest.json       # 风格库
└── product-info.md          # 产品信息
```

## 触发
每天 UTC 14:00 自动运行（上海 22:00 / 纽约 09:00）
也可手动触发 workflow_dispatch

## GitHub Secrets 配置
| Secret | 值 |
|--------|------|
| R2_ENDPOINT | https://01e6ed9ffab6e02dbd7364690125eb2e.r2.cloudflarestorage.com |
| R2_ACCESS_KEY | aa6d21bfaa39dfe02a6bbbe895ca72b1 |
| R2_SECRET_KEY | c48d5158daef80ce42167833678253eb7125b20c36e0bec2a9bb742f509bb8b7 |
| R2_BUCKET | barekey-content |
| DEEPSEEK_API_KEY | sk-f6108c01026249de89ac25f7ab72e935 |
| BUFFER_ACCESS_TOKEN | o-W1aAfk_qVxC39I9551z4u5kmloMThVD3u-ozQ0Uzw |

## 发布逻辑
- 全库图片顺序轮转，状态存 R2 `_system/pinterest-rotation-state.json`
- 每7个 Pin 一个周期：第1个带主页链接，第4个带产品页链接，其余不带链接
- 发布走 Buffer GraphQL API → Buffer 推送到 Pinterest（无需 Pinterest API 审核）
