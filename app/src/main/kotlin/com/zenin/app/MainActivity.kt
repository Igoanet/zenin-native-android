package com.zenin.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import com.zenin.app.nav.AppNavigation
import com.zenin.app.ui.theme.ZeninTheme

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        // Start SSE if already logged in
        val app = application as ZeninApp
        val token = app.authRepository.currentToken()
        if (token != null) {
            app.sseManager.start(token, app.appScope)
        }

        setContent {
            ZeninTheme {
                AppNavigation()
            }
        }
    }
}
