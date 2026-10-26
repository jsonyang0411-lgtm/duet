# 达人副驾驶的业务资料目录（模板）

把这个文件夹整个复制到一个**单独的位置**（不要放在任何代码仓库里，也不要直接用主目录），例如：

```
cp -R tools/claude_relay/prm_workspace_template ~/Documents/prm_workspace
```

然后在 Mac 的 `~/.claude-relay/config.json` 里写 `"prm_workspace": "~/Documents/prm_workspace"`，重启 worker。

- `kb/`：知识库，运营维护。Claude 只能读。合作政策、报价和佣金规则、寄样规则、素材规范、常见问答都写在这里；**涉及钱的回答只能引用这里的原文**。
- `deals/`：每个达人一份案卷（`<编号>.md`），第一次有消息时自动建，Claude 每次起草都会更新。可以手动补充备注。
- `assets/`：样品图、brief 等素材。草稿里列出的文件只会从这里取，随回复一起发给达人（运营在控制台确认之后）。
- `inbox/`：达人发来的图片和文件，worker 自动下载，7 天后删除。
- `kb/products.md`、`kb/products.json`：配了平台联动（config.json 的 `platforms`）时，由 `python3 tools/claude_relay/worker.py --sync-products` 从小红书 / 抖音 / 淘宝读出来的在售商品目录，不要手改。草稿里的平台动作只能用这里的商品；佣金、专属价、预算的规则照样写在 `kb/` 的合作政策里。

Claude 在这里只有读写文件的工具，不能运行命令、不能上网，也碰不到这个目录以外的任何东西。
