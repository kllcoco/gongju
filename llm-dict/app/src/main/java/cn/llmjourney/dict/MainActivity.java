package cn.llmjourney.dict;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.View;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/** 一个全屏 WebView，加载打包在 assets 里的离线字典。 */
public class MainActivity extends Activity {
    private static final String HOME = "file:///android_asset/www/index.html";
    private static final int LIGHT = 0xFFFFFFFF;
    private static final int DARK = 0xFF161B22;
    private WebView web;
    private boolean loaded = false;
    private String pendingGo = null;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        boolean night = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
        web = new WebView(this);
        web.setBackgroundColor(night ? DARK : LIGHT);
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        setContentView(web);
        applyBars(night);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setTextZoom(100);              // 字号由应用里的四档来管，不跟系统字体缩放叠加
        s.setAllowFileAccess(false);     // android_asset 不受这个开关影响
        s.setAllowContentAccess(false);
        s.setSupportZoom(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);

        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        pendingGo = getIntent() == null ? null : getIntent().getStringExtra("go");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                loaded = true;
                goPending();
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                return !"file".equals(u.getScheme());   // 离线应用，不打开任何外部链接
            }
        });
        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(HOME);
        }
    }

    /** 点了通知栏闪卡，打开 App 后直接进闪卡页。 */
    private void goPending() {
        if (pendingGo == null || !loaded) return;
        final String go = pendingGo;
        pendingGo = null;
        web.postDelayed(new Runnable() {
            @Override
            public void run() {
                web.evaluateJavascript("(function(){var n=0;(function t(){if(window.__ready&&window.__ready()){window.__nav('" + go + "');}else if(n++<50){setTimeout(t,100);}})();})()", null);
            }
        }, 200);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String go = intent.getStringExtra("go");
        if (go != null) {
            pendingGo = go;
            goPending();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 在通知栏里评过分的话，回到 App 时把最新的卡片状态读进来
        if (loaded) web.evaluateJavascript("window.__fcReload&&window.__fcReload()", null);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == 7) {
            boolean ok = grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            web.evaluateJavascript("window.__fcPerm&&window.__fcPerm(" + ok + ")", null);
        }
    }

    private void applyBars(boolean dark) {
        Window w = getWindow();
        w.setStatusBarColor(dark ? DARK : LIGHT);
        w.setNavigationBarColor(dark ? DARK : LIGHT);
        int flags = dark ? 0 : (View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        w.getDecorView().setSystemUiVisibility(flags);
        if (web != null) web.setBackgroundColor(dark ? DARK : LIGHT);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    public void onBackPressed() {
        web.evaluateJavascript("(function(){try{return window.__back&&window.__back()?1:0}catch(e){return 0}})()", new ValueCallback<String>() {
            @Override
            public void onReceiveValue(String value) {
                if (!"1".equals(value)) finish();
            }
        });
    }

    private class Bridge {
        @JavascriptInterface
        public void copy(String text) {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) cm.setPrimaryClip(ClipData.newPlainText("code", text));
        }

        @JavascriptInterface
        public String fcLoad() {
            return FlashCore.read(MainActivity.this);
        }

        @JavascriptInterface
        public void fcSave(String json) {
            FlashCore.write(MainActivity.this, json);
            FlashCore.reschedule(MainActivity.this);
        }

        @JavascriptInterface
        public void fcAsk() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    if (FlashCore.permitted(MainActivity.this)) {
                        web.evaluateJavascript("window.__fcPerm&&window.__fcPerm(true)", null);
                    } else {
                        requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 7);
                    }
                }
            });
        }

        @JavascriptInterface
        public String fcTest() {
            return FlashCore.test(MainActivity.this);
        }

        /** 把学习日志存进手机的“下载”文件夹，再打开分享面板。 */
        @JavascriptInterface
        public String fcExport(String name, final String text) {
            try {
                ContentValues v = new ContentValues();
                v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                v.put(MediaStore.MediaColumns.MIME_TYPE, "text/plain");
                v.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                final Uri u = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
                if (u == null) return "fail";
                try (OutputStream os = getContentResolver().openOutputStream(u)) {
                    if (os == null) return "fail";
                    os.write(text.getBytes(StandardCharsets.UTF_8));
                }
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        try {
                            Intent send = new Intent(Intent.ACTION_SEND);
                            send.setType("text/plain");
                            send.putExtra(Intent.EXTRA_STREAM, u);
                            if (text.length() < 100000) send.putExtra(Intent.EXTRA_TEXT, text);
                            send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                            startActivity(Intent.createChooser(send, "发送学习日志"));
                        } catch (Exception ignored) {
                        }
                    }
                });
                return "ok";
            } catch (Exception e) {
                return "fail";
            }
        }

        @JavascriptInterface
        public void setDark(final boolean dark) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() { applyBars(dark); }
            });
        }
    }
}
