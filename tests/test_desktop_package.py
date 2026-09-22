import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop_v2"


class DesktopPackageTests(unittest.TestCase):
    def test_customer_documents_are_packaged(self):
        package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
        resources = package["build"]["extraResources"]
        expected = {
            "docs/阿里云百炼APIKey获取图文教程.pdf",
            "docs/闲鱼卡券AI客服-V3.6-客户使用说明书-0.11.11.pdf",
        }
        packaged = {entry["from"] for entry in resources}
        self.assertTrue(expected.issubset(packaged))
        for relative_path in expected:
            document = DESKTOP / relative_path
            self.assertTrue(document.is_file(), relative_path)
            self.assertGreater(document.stat().st_size, 10_000, relative_path)

    def test_auto_update_channel_is_configured(self):
        package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
        self.assertEqual("0.11.35", package["version"])
        self.assertIn("electron-updater", package["dependencies"])
        self.assertEqual(
            [{"provider": "generic", "url": "https://download.yituan123.com/v3.6"}],
            package["build"]["publish"],
        )
        self.assertIn("推广模式", package["build"]["releaseInfo"]["releaseNotes"])
        self.assertIn("混合发卡", package["build"]["releaseInfo"]["releaseNotes"])
        self.assertIn("SKU知识归纳", package["build"]["releaseInfo"]["releaseNotes"])
        self.assertIn("esbuild electron/updater-entry.cjs", package["scripts"]["build:updater"])
        self.assertTrue((DESKTOP / "electron" / "updater-entry.cjs").is_file())
        self.assertTrue((DESKTOP / "electron" / "update-policy.cjs").is_file())

    def test_windows_and_tray_icons_are_packaged(self):
        package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
        icon = DESKTOP / "assets" / "xianyu-ai.ico"
        self.assertTrue(icon.is_file())
        self.assertGreater(icon.stat().st_size, 1000)
        self.assertEqual("assets/xianyu-ai.ico", package["build"]["win"]["icon"])
        self.assertEqual("assets/xianyu-ai.ico", package["build"]["nsis"]["installerIcon"])
        self.assertIn(
            {"from": "assets/xianyu-ai.ico", "to": "tray-icon.ico"},
            package["build"]["extraResources"],
        )
        main = (DESKTOP / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("path.join(process.resourcesPath, 'tray-icon.ico')", main)
        self.assertNotIn("data:image/svg+xml", main)

    def test_backend_package_avoids_slow_onefile_extraction(self):
        build = (ROOT / "build-v3.ps1").read_text(encoding="utf-8")
        main = (DESKTOP / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("--onedir", build)
        self.assertNotIn("--onefile", build)
        self.assertIn("'xianyu-cloud-preview-backend',", main)
        self.assertIn("Date.now() - started < 90000", main)
        self.assertIn("backend.log", main)

    def test_local_backend_requests_do_not_use_fetch_or_proxy(self):
        main = (DESKTOP / "electron" / "main.cjs").read_text(encoding="utf-8")
        client = (DESKTOP / "electron" / "local-backend-client.cjs").read_text(encoding="utf-8")
        self.assertIn("requestLocalBackend(Number(record.port)", main)
        self.assertNotIn("fetch(`http://127.0.0.1:${backendPort}", main)
        self.assertIn("hostname: '127.0.0.1'", client)
        self.assertIn("agent: false", client)

    def test_active_backend_requests_use_child_process_rpc(self):
        main = (DESKTOP / "electron" / "main.cjs").read_text(encoding="utf-8")
        backend = (ROOT / "v2_backend.py").read_text(encoding="utf-8")
        self.assertIn("requestBackendRpc(method, requestPath, body)", main)
        self.assertIn("stdio: ['pipe', 'pipe', 'pipe']", main)
        self.assertNotIn("requestLocalBackend(backendPort", main)
        self.assertIn("__XIANYU_RPC__", main)
        self.assertIn("__XIANYU_RPC__", backend)

    def test_tray_click_recreates_and_brings_window_to_front(self):
        main = (DESKTOP / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("tray.on('click', showMainWindow)", main)
        self.assertIn("tray.on('double-click', showMainWindow)", main)
        self.assertIn("windowCreatePromise = createWindow()", main)
        self.assertIn("window.setAlwaysOnTop(true)", main)
        self.assertIn("window.moveTop()", main)

    def test_r2_release_script_preserves_safe_upload_order(self):
        script = (ROOT / "deploy" / "deploy-r2-release.ps1").read_text(encoding="utf-8")
        installer = script.index("$installerPath")
        blockmap = script.index("$blockmapPath", installer)
        latest = script.index("$latestPath", blockmap)
        self.assertLess(installer, blockmap)
        self.assertLess(blockmap, latest)
        self.assertIn('CacheControl = "no-store, max-age=0"', script)

    def test_v36_ui_has_no_waiting_payment_auto_notice_rule(self):
        source = (DESKTOP / "src" / "App.vue").read_text(encoding="utf-8")
        self.assertNotIn("order_payment_notice_enabled", source)
        self.assertNotIn("order_notice_enabled", source)
        self.assertNotIn("拍下未付款", source)

    def test_ai_draft_ui_never_displays_effective_summary_as_a_new_draft(self):
        source = (DESKTOP / "src" / "App.vue").read_text(encoding="utf-8")
        self.assertIn("ai_draft_summary: product.ai_draft_summary || ''", source)
        self.assertNotIn(
            "ai_draft_summary: product.ai_draft_summary || product.ai_summary",
            source,
        )
        self.assertIn("尚未生成AI草稿", source)

    def test_goofish_verification_is_visible_and_retryable_in_the_frontend(self):
        source = (DESKTOP / "src" / "App.vue").read_text(encoding="utf-8")
        main = (DESKTOP / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn("verification_required", source)
        self.assertIn("验证完成，重新连接", source)
        self.assertIn("/service/retry-auth", source)
        self.assertNotIn("window.alert", source)
        self.assertIn("showVerificationPrompt", source)
        self.assertIn("verification:prompt", main)
        self.assertIn("dialog.showMessageBox", main)
        self.assertIn("isAllowedGoofishNavigation", main)
        self.assertIn("taobao.com", main)

    def test_slow_backend_calls_do_not_block_ui_status_requests(self):
        source = (DESKTOP / "src" / "App.vue").read_text(encoding="utf-8")
        backend = (ROOT / "v2_backend.py").read_text(encoding="utf-8")
        self.assertIn("threading.Thread(", backend)
        self.assertIn("target=_handle_stdio_rpc", backend)
        self.assertIn("max_retries=0", backend)
        self.assertIn("Promise.allSettled", source)
        self.assertIn("loading && route !== 'workspace'", source)
        self.assertIn("verificationRetrying", source)
        self.assertIn("if (refreshPromise) return refreshPromise", source)
        self.assertIn("refreshServiceStatus", source)
        self.assertIn("refresh(true), getConfig()", source)
        self.assertNotIn("setInterval(() => refresh(true)", source)

    def test_backend_rpc_is_code_page_independent(self):
        main = (DESKTOP / "electron" / "main.cjs").read_text(encoding="utf-8")
        codec = (DESKTOP / "electron" / "rpc-codec.cjs").read_text(encoding="utf-8")
        backend = (ROOT / "v2_backend.py").read_text(encoding="utf-8")
        self.assertIn('json.dumps(result, ensure_ascii=True)', backend)
        self.assertIn('return {"reply": "连接成功"}', backend)
        self.assertIn("stringifyAsciiJson({ id, method, path: requestPath, body })", main)
        self.assertIn("'ascii'", main)
        self.assertIn("character.charCodeAt(0)", codec)

    def test_store_import_is_staged_out_of_temporary_directories(self):
        main = (DESKTOP / "electron" / "main.cjs").read_text(encoding="utf-8")
        policy = (DESKTOP / "electron" / "import-file-policy.cjs").read_text(encoding="utf-8")
        self.assertIn("stageStoreImport(result.filePaths[0]", main)
        self.assertIn("fs.copyFileSync(source, target)", policy)
        self.assertIn("path.resolve(userDataPath, 'imports')", policy)


if __name__ == "__main__":
    unittest.main()
