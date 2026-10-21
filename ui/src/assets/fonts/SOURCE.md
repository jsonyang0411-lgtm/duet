# 前端打包的字体

三款字体都是 SIL Open Font License 1.1，各自目录里有原样的 `OFL.txt`。文件直接取自本机的口袋效果器设计稿
（`口袋效果器-界面设计稿/prototype/assets/fonts/`，那边的 `SOURCE.txt` 记着下载地址、版本和 sha256），没有再改动。

| 字体 | 上游 | 文件 |
|---|---|---|
| Noto Sans SC（思源黑体）2.004 | https://github.com/notofonts/noto-cjk | Regular / Medium / Bold 子集 WOFF2：ASCII + GB2312 + JIS 第一水准汉字 + 假名 + CJK 标点和全角字符。子集外的字按字回退到系统黑体（微软雅黑 / 苹方） |
| Barlow Condensed 1.408 | https://github.com/google/fonts（ofl/barlowcondensed） | Light / SemiBold / Bold |
| JetBrains Mono | https://github.com/JetBrains/JetBrainsMono | Regular / SemiBold / Bold |

控制台不从网络加载任何字体，桌面程序离线也能显示。
