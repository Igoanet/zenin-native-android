package com.zenin.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.zenin.app.ui.components.*
import com.zenin.app.ui.theme.*
import com.zenin.app.viewmodel.SettingsViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    onBack: () -> Unit,
    vm: SettingsViewModel = viewModel()
) {
    val currentUrl by vm.apiUrl.collectAsStateWithLifecycle()
    val saved by vm.saved.collectAsStateWithLifecycle()

    var urlInput by remember(currentUrl) { mutableStateOf(currentUrl) }

    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(saved) {
        if (saved) {
            snackbarHostState.showSnackbar("API URL saved")
            vm.resetSaved()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text("SETTINGS", style = TextStyle(
                        fontFamily = OrbitronFamily, fontWeight = FontWeight.Black,
                        fontSize = 15.sp, letterSpacing = 3.sp, color = Cyan
                    ))
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Default.ArrowBack, contentDescription = "Back", tint = CyanDim)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = BgDark)
            )
        },
        snackbarHost = { SnackbarHost(snackbarHostState) },
        containerColor = BgDark
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(24.dp)
        ) {
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("API SERVER")
                Spacer(Modifier.height(12.dp))
                Text(
                    "The URL of your ZENIN API server. Change this if you deploy to a custom domain.",
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurfaceDim)
                )
                Spacer(Modifier.height(16.dp))
                ZeninTextField(
                    value = urlInput,
                    onValueChange = { urlInput = it },
                    label = "API BASE URL",
                    placeholder = "https://your-server.com/api",
                    singleLine = true,
                    leadingIcon = { Icon(Icons.Default.Link, contentDescription = null, tint = CyanDim, modifier = Modifier.size(18.dp)) }
                )
                Spacer(Modifier.height(16.dp))
                ZeninButton(
                    text = "SAVE",
                    onClick = { if (urlInput.isNotBlank()) vm.saveUrl(urlInput) },
                    modifier = Modifier.fillMaxWidth(),
                    enabled = urlInput != currentUrl && urlInput.isNotBlank()
                )
            }

            Spacer(Modifier.height(16.dp))

            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("ABOUT")
                Spacer(Modifier.height(8.dp))
                InfoRow("App", "ZENIN Mobile v2.0")
                InfoRow("Build", "Native Kotlin")
                InfoRow("UI", "Jetpack Compose")
                InfoRow("Protocol", "OkHttp + SSE")
            }
        }
    }
}
