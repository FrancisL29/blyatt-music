package com.blyatt.app;

import android.app.Dialog;
import android.net.Uri;
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
    // directo al login de Google (el mismo enlace que el boton "Iniciar sesion" de YT Music): sin pasar por
    // la portada de YT Music. Tras el login Google redirige a music.youtube.com y se captura la cookie
    private static final String SIGNIN = "https://accounts.google.com/ServiceLogin?service=youtube&uilel=3&passive=true"
        + "&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue%26app%3Ddesktop%26hl%3Des"
        + "%26next%3Dhttps%253A%252F%252Fmusic.youtube.com%252F&hl=es";
    // dominios con cookies de la cuenta de Google/YouTube (el CookieManager es UNICO para toda la app:
    // borrar todo mataria tambien la cookie de sesion de Cloudflare Access y la `bid` de blyatt.stream)
    private static final String[] GOOGLE_URLS = {
        "https://accounts.google.com", "https://www.google.com", "https://google.com", "https://myaccount.google.com",
        "https://music.youtube.com", "https://www.youtube.com", "https://youtube.com", "https://m.youtube.com",
        "https://accounts.youtube.com"
    };

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
            web.loadUrl(SIGNIN);
        });
    }

    /** Cierra la sesion de Google SOLO en el WebView de la app (para poder entrar con otra cuenta). */
    @PluginMethod
    public void logout(PluginCall call) {
        getActivity().runOnUiThread(() -> {
            CookieManager cm = CookieManager.getInstance();
            String past = "=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/";
            for (String u : GOOGLE_URLS) {
                String c = cm.getCookie(u);
                if (c == null) continue;
                String host = Uri.parse(u).getHost();
                String base = host.endsWith("google.com") ? "google.com" : "youtube.com";
                for (String part : c.split(";")) {
                    String name = part.split("=", 2)[0].trim();
                    if (name.isEmpty()) continue;
                    // una cookie solo se borra si coinciden dominio y ruta: se prueban las variantes posibles
                    cm.setCookie(u, name + past + "; Secure");                        // de host (incluye __Host-)
                    if (name.startsWith("__Host-")) continue;
                    cm.setCookie(u, name + past + "; Secure; Domain=." + base);       // de dominio
                    cm.setCookie(u, name + past + "; Domain=." + base);
                    cm.setCookie(u, name + past);
                }
            }
            cm.flush();
            String left = cm.getCookie(MUSIC);
            JSObject r = new JSObject();
            r.put("cleared", left == null || !left.contains("SAPISID="));
            call.resolve(r);
        });
    }

    private void close() {
        if (web != null) { web.destroy(); web = null; }
        if (dialog != null) { dialog.dismiss(); dialog = null; }
    }
}
