package com.blyatt.app;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(GoogleLoginPlugin.class);
        registerPlugin(MediaControlsPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
