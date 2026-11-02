package com.blyatt.app;

import android.accounts.AccountManager;
import android.app.Activity;
import android.app.Dialog;
import android.content.Intent;
import android.net.Uri;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;

import androidx.activity.result.ActivityResult;

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

    // primero el selector de cuentas de Android (las cuentas de Google del movil, sin permisos): el login web
    // se abre con ese correo ya escrito y pasa solo al siguiente paso (contrasena o "confirma en tu movil").
    // Google no da cookies de YouTube Music a partir de una cuenta del sistema: el ultimo paso sigue siendo web
    @PluginMethod
    public void login(PluginCall call) {
        try {
            Intent pick = AccountManager.newChooseAccountIntent(null, null, new String[]{"com.google"},
                null, null, null, null);
            startActivityForResult(call, pick, "accountPicked");
        } catch (Exception e) {   // sin selector (ROM rara): login web de siempre
            openLogin(call, null);
        }
    }

    @ActivityCallback
    private void accountPicked(PluginCall call, ActivityResult result) {
        if (call == null) return;
        Intent d = result.getData();
        String email = result.getResultCode() == Activity.RESULT_OK && d != null
            ? d.getStringExtra(AccountManager.KEY_ACCOUNT_NAME) : null;
        openLogin(call, email);   // cancelado: login web normal (puede ser una cuenta que no esta en el movil)
    }

    private void openLogin(PluginCall call, String email) {
        pending = call;
        final boolean[] autoNext = { email != null };
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
                    if (autoNext[0] && url.contains("/signin/identifier")) {   // correo ya puesto: "Siguiente"
                        autoNext[0] = false;
                        v.evaluateJavascript("setTimeout(function(){var b=document.querySelector('#identifierNext button')"
                            + "||document.querySelector('#identifierNext');if(b)b.click();},400)", null);
                    }
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
            web.loadUrl(email == null ? SIGNIN : SIGNIN + "&Email=" + Uri.encode(email));
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
