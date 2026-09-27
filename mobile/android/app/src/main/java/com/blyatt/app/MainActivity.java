package com.blyatt.app;

import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
import android.webkit.WebView;

import androidx.activity.EdgeToEdge;
import androidx.activity.SystemBarStyle;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.WebViewListener;

import java.util.Locale;

public class MainActivity extends BridgeActivity {
    // alto de las barras del sistema en dp (lo que la pagina usa como --sat / --sab)
    private int insTop = 0, insBottom = 0;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(GoogleLoginPlugin.class);
        registerPlugin(MediaControlsPlugin.class);
        super.onCreate(savedInstanceState);

        // Pantalla completa: la app se dibuja bajo la barra de estado y la de navegacion (transparentes, iconos
        // claros). El SystemBars de Capacitor queda en insetsHandling "disable" (capacitor.config.json): con
        // WebView < 140 rellenaba el hueco con padding y la barra se veia negra.
        EdgeToEdge.enable(this, SystemBarStyle.dark(Color.TRANSPARENT), SystemBarStyle.dark(Color.TRANSPARENT));

        WebView wv = getBridge().getWebView();
        View host = (View) wv.getParent();
        ViewCompat.setOnApplyWindowInsetsListener(host, (v, insets) -> {
            Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());
            boolean kb = insets.isVisible(WindowInsetsCompat.Type.ime());
            // en pantalla completa el teclado ya no encoge la ventana: se encoge la WebView para no tapar el campo
            v.setPadding(0, 0, 0, kb ? insets.getInsets(WindowInsetsCompat.Type.ime()).bottom : 0);
            float d = getResources().getDisplayMetrics().density;
            insTop = Math.round(bars.top / d);
            insBottom = kb ? 0 : Math.round(bars.bottom / d);
            injectInsets();
            return WindowInsetsCompat.CONSUMED;
        });
        // cada carga de pagina (la app es remota) pierde el estilo inyectado: se vuelve a poner
        getBridge().addWebViewListener(new WebViewListener() {
            @Override public void onPageCommitVisible(WebView view, String url) { injectInsets(); }
            @Override public void onPageLoaded(WebView webView) { injectInsets(); }
        });
        ViewCompat.requestApplyInsets(host);
    }

    private void injectInsets() {
        WebView wv = getBridge() != null ? getBridge().getWebView() : null;
        if (wv == null) return;
        String js = String.format(Locale.US,
            "try{var s=document.documentElement.style;s.setProperty('--sat','%dpx');s.setProperty('--sab','%dpx');}catch(e){}",
            insTop, insBottom);
        wv.post(() -> wv.evaluateJavascript(js, null));
    }
}
