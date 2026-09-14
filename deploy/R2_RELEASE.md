# V3.6 R2 发布

先在 Cloudflare R2 创建 `xianyu-releases`，并把自定义域名
`download.yituan123.com` 绑定到该 Bucket。然后从仓库根目录运行：

```powershell
.\deploy\deploy-r2-release.ps1 -DryRun
.\deploy\deploy-r2-release.ps1
```

脚本会校验 `latest.yml` 指向的安装包和 `.blockmap`，并严格按照以下顺序上传：

1. 带版本号的安装包，长期缓存；
2. 对应 `.blockmap`，长期缓存；
3. `latest.yml`，禁用缓存。

同版本安装包禁止覆盖；内容变化时必须先增加版本号并重新打包。
