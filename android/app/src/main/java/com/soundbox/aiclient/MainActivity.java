package com.soundbox.aiclient;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.graphics.drawable.GradientDrawable;
import android.net.http.SslError;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.util.Log;
import android.view.View;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;

public class MainActivity extends Activity {
    private static final String TAG = "VoiceClient";
    private static final int REQ_MIC = 1001;
    private static final String PREFS = "voice_client";
    private static final String KEY_URL = "server_url";
    private static final long LOAD_TIMEOUT_MS = 30000;

    private WebView webView;
    private EditText serverUrl;
    private TextView statusChip;
    private TextView statusMessage;
    private TextView emptyTitle;
    private TextView emptyBody;
    private ProgressBar progress;
    private Button btnLoad;
    private Button btnRetry;
    private View emptyState;

    private String pendingUrl;
    private boolean pageReady;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable loadTimeout = this::onLoadTimeout;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webView);
        serverUrl = findViewById(R.id.serverUrl);
        statusChip = findViewById(R.id.statusChip);
        statusMessage = findViewById(R.id.statusMessage);
        emptyState = findViewById(R.id.emptyState);
        emptyTitle = findViewById(R.id.emptyTitle);
        emptyBody = findViewById(R.id.emptyBody);
        progress = findViewById(R.id.progress);
        btnLoad = findViewById(R.id.btnLoad);
        btnRetry = findViewById(R.id.btnRetry);

        String saved = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_URL, null);
        if (!TextUtils.isEmpty(saved)) {
            serverUrl.setText(saved);
        }

        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);
        ws.setSupportZoom(false);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);

        if (isDebuggable()) {
            WebView.setWebContentsDebuggingEnabled(true);
        }

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return false;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                pageReady = false;
                setState(State.LOADING, "Loading " + hostOf(url) + "…");
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                mainHandler.removeCallbacks(loadTimeout);
                pageReady = true;
                webView.setVisibility(View.VISIBLE);
                emptyState.setVisibility(View.GONE);
                progress.setProgress(100);
                setState(State.READY, "Connected to " + hostOf(url));
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (!request.isForMainFrame()) return;
                String desc = error != null ? error.getDescription().toString() : "network error";
                int code = error != null ? error.getErrorCode() : -1;
                Log.e(TAG, "Load error code=" + code + " desc=" + desc + " url=" + request.getUrl());
                mainHandler.removeCallbacks(loadTimeout);
                showError("Cannot reach server (" + desc + ").\nCheck PC IP, server running, and Wi-Fi same network.");
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                Log.w(TAG, "SSL error allowed (dev cert): " + error);
                mainHandler.post(() -> statusMessage.setText("Accepting dev certificate…"));
                handler.proceed();
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> {
                    try {
                        for (String r : request.getResources()) {
                            if (!PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)
                                    && !PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) {
                                request.deny();
                                return;
                            }
                        }
                        request.grant(request.getResources());
                        Log.i(TAG, "Granted: " + String.join(",", request.getResources()));
                    } catch (Exception e) {
                        Log.e(TAG, "Permission grant failed", e);
                    }
                });
            }

            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                if (!pageReady) {
                    progress.setProgress(newProgress);
                    statusMessage.setText("Loading " + newProgress + "%");
                }
            }
        });

        btnLoad.setOnClickListener(v -> loadServer());
        btnRetry.setOnClickListener(v -> loadServer());
        setState(State.IDLE, "Enter your PC server URL, then tap Load");
    }

    private enum State { IDLE, LOADING, READY, ERROR }

    private void setState(State state, String message) {
        statusMessage.setText(message);
        GradientDrawable chip = new GradientDrawable();
        chip.setCornerRadius(99f);
        int bg;
        String label;
        switch (state) {
            case LOADING:
                bg = getColor(R.color.chip_loading);
                label = "Loading";
                progress.setVisibility(View.VISIBLE);
                btnLoad.setEnabled(false);
                break;
            case READY:
                bg = getColor(R.color.chip_ready);
                label = "Online";
                progress.setVisibility(View.GONE);
                btnLoad.setEnabled(true);
                break;
            case ERROR:
                bg = getColor(R.color.chip_error);
                label = "Error";
                progress.setVisibility(View.GONE);
                btnLoad.setEnabled(true);
                break;
            default:
                bg = getColor(R.color.chip_idle);
                label = "Ready";
                progress.setVisibility(View.GONE);
                btnLoad.setEnabled(true);
                break;
        }
        chip.setColor(bg);
        statusChip.setBackground(chip);
        statusChip.setText(label);
    }

    private void showError(String message) {
        setState(State.ERROR, message.replace('\n', ' '));
        webView.setVisibility(View.INVISIBLE);
        emptyState.setVisibility(View.VISIBLE);
        emptyTitle.setText(R.string.error_title);
        emptyBody.setText(message);
        btnRetry.setVisibility(View.VISIBLE);
    }

    private void onLoadTimeout() {
        if (pageReady) return;
        Log.e(TAG, "Load timeout for " + pendingUrl);
        showError("Timed out after " + (LOAD_TIMEOUT_MS / 1000) + "s.\n"
                + "1) Server running on PC?\n"
                + "2) Firewall allows TCP 8443?\n"
                + "3) Phone and PC on same Wi-Fi?\n"
                + "URL: " + pendingUrl);
        try {
            webView.stopLoading();
        } catch (Exception ignored) {
        }
    }

    private boolean isDebuggable() {
        return (getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0;
    }

    private void loadServer() {
        String url = serverUrl.getText().toString().trim();
        if (TextUtils.isEmpty(url)) {
            url = getString(R.string.default_server_url);
            serverUrl.setText(url);
        }
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "https://" + url;
            serverUrl.setText(url);
        }
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_URL, url).apply();
        pendingUrl = url;
        ensureMicThenLoad();
    }

    private void ensureMicThenLoad() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            doLoad();
        } else {
            setState(State.LOADING, "Microphone permission required…");
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_MIC) {
            doLoad();
        }
    }

    private void doLoad() {
        if (pendingUrl == null) return;
        pageReady = false;
        mainHandler.removeCallbacks(loadTimeout);
        mainHandler.postDelayed(loadTimeout, LOAD_TIMEOUT_MS);
        setState(State.LOADING, "Connecting to " + hostOf(pendingUrl) + "…");
        progress.setProgress(5);
        emptyState.setVisibility(View.GONE);
        btnRetry.setVisibility(View.GONE);
        webView.setVisibility(View.INVISIBLE);
        webView.loadUrl(pendingUrl);
    }

    private static String hostOf(String url) {
        try {
            java.net.URI uri = new java.net.URI(url);
            String host = uri.getHost();
            return host != null ? host : url;
        } catch (Exception e) {
            return url;
        }
    }

    @Override
    public void onBackPressed() {
        if (pageReady && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        webView.onResume();
    }

    @Override
    protected void onPause() {
        webView.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        mainHandler.removeCallbacksAndMessages(null);
        webView.destroy();
        super.onDestroy();
    }
}
