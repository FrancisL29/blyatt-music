package com.blyatt.app;

import android.app.Dialog;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

// Abre music.youtube.com en un WebView propio (cookies compartidas via CookieManager del sistema).
// Cuando la cookie de music.youtube.com contiene SAPISID (login completo), cierra y devuelve
// {cookie, ua} para que el frontend la mande a POST /auth/cookie del servidor.
@CapacitorPlugin(name = "GoogleLogin")
public class GoogleLoginPlugin extends Plugin {

    // UA de Chrome movil real: Google bloquea logins desde UAs de WebView ("disallowed_useragent")
    private static final String CHROME_UA =
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36";
    private static final String MUSIC = "https://music.youtube.com";

    private Dialog dialog;
    private WebView web;
    private PluginCall pending;

    @PluginMethod
    public void login(PluginCall call) {
        pending = call;
        getActivity().runOnUiThread(() -> {
            dialog = new Dialog(getActivity(), android.R.style.Theme_Black_NoTitleBar_Fullscreen);
            web = new WebView(getActivity());
            WebSettings s = web.getSettings();
            s.setJavaScriptEnabled(true);
            s.setDomStorageEnabled(true);
            s.setUserAgentString(CHROME_UA);
            CookieManager cm = CookieManager.getInstance();
            cm.setAcceptCookie(true);
            cm.setAcceptThirdPartyCookies(web, true);
            web.setWebViewClient(new WebViewClient() {
                @Override
                public void onPageFinished(WebView v, String url) {
                    String c = CookieManager.getInstance().getCookie(MUSIC);
                    if (c != null && c.contains("SAPISID=") && c.contains("__Secure-3PSID")) {
                        CookieManager.getInstance().flush();
                        if (pending != null) {
                            JSObject r = new JSObject();
                            r.put("cookie", c);
                            r.put("ua", CHROME_UA);
                            pending.resolve(r);
                            pending = null;
                        }
                        close();
                    }
                }
            });
            dialog.setContentView(web);
            dialog.setOnCancelListener(d -> {
                if (pending != null) { pending.reject("cancelado"); pending = null; }
                close();
            });
            dialog.show();
            // si ya hay sesion del sistema en music.youtube.com entra directo; si no, YT redirige al login de Google
            web.loadUrl(MUSIC);
        });
    }

    private void close() {
        if (web != null) { web.destroy(); web = null; }
        if (dialog != null) { dialog.dismiss(); dialog = null; }
    }
}
