# XianyuAutoAgent 维护规则

开始任何修改前，完整阅读 `HANDOFF_V3_5_V3_6.md`。

## 仓库边界

- 当前目录是 V3.5 本地版，维护分支为 `local-gui-v1`。
- V3.6 云端内测版在相邻独立工作树 `..\XianyuAutoAgent-cloud-preview-v3.6`，维护分支为 `cloud-preview-v3.6`。
- 两个分支没有共同 merge base。禁止直接 merge；跨版本修复必须人工移植并分别验证。
- 不得覆盖、清理或顺手提交不属于当前任务的既有修改，尤其是 V3.6 工作树中的未提交文件。

## 工作方式

1. 先执行 `git status --short --branch`，确认分支、版本号和脏文件。
2. 优先修确定性 SKU、门店、价格、数量和上下文逻辑，不让模型发明业务事实。
3. 修改业务逻辑时补回归测试；V3.5 完整测试命令：

   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -v
   ```

4. 提交前执行 `git diff --check` 和完整测试。
5. 只有用户明确要求时才升版本、打安装包、覆盖安装或发布自动更新。
6. 不提交 `.env`、API Key、Cookie、密码、授权数据库、用户数据库或真实聊天数据。

## 发布

- V3.5 构建：`powershell -ExecutionPolicy Bypass -File .\build-v3.ps1`
- V3.6 构建：在 V3.6 工作树运行同名脚本，产物还包含 `.blockmap` 和 `latest.yml`。
- 每次发布记录版本号、功能提交、版本戳提交、测试结果、安装包路径和 SHA-256。
- V3.6 禁止覆盖同版本远端安装包，上传顺序必须是安装包、`.blockmap`、`latest.yml`。
