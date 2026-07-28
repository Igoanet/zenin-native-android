package com.zenin.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
    onPanelsClick: () -> Unit,
    vm: SettingsViewModel = viewModel()
) {
    val currentUrl by vm.apiUrl.collectAsStateWithLifecycle()
    val saved by vm.saved.collectAsStateWithLifecycle()
    val profile by vm.profile.collectAsStateWithLifecycle()
    val profileMsg by vm.profileMsg.collectAsStateWithLifecycle()
    val pwMsg by vm.pwMsg.collectAsStateWithLifecycle()
    val pwBusy by vm.pwBusy.collectAsStateWithLifecycle()

    var urlInput by remember(currentUrl) { mutableStateOf(currentUrl) }
    var nameInput by remember(profile?.name) { mutableStateOf(profile?.name ?: "") }
    var currentPw by remember { mutableStateOf("") }
    var newPw by remember { mutableStateOf("") }

    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(saved) {
        if (saved) {
            snackbarHostState.showSnackbar("API URL saved")
            vm.resetSaved()
        }
    }
    LaunchedEffect(profileMsg) {
        profileMsg?.let {
            snackbarHostState.showSnackbar(it)
            vm.clearMessages()
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
                .verticalScroll(rememberScrollState())
                .padding(24.dp)
        ) {
            // ─── Profile (parity with web) ──────────────────────────────────
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("PROFILE")
                Spacer(Modifier.height(12.dp))
                InfoRow("User ID", profile?.userId ?: "…")
                InfoRow("Role", profile?.role ?: "…")
                Spacer(Modifier.height(12.dp))
                ZeninTextField(
                    value = nameInput,
                    onValueChange = { nameInput = it },
                    label = "DISPLAY NAME",
                    placeholder = "Your name",
                    singleLine = true,
                    leadingIcon = { Icon(Icons.Default.Person, contentDescription = null, tint = CyanDim, modifier = Modifier.size(18.dp)) }
                )
                Spacer(Modifier.height(16.dp))
                ZeninButton(
                    text = "SAVE NAME",
                    onClick = { vm.saveName(nameInput) },
                    modifier = Modifier.fillMaxWidth(),
                    enabled = nameInput.trim().isNotBlank() && nameInput.trim() != (profile?.name ?: "")
                )
            }

            Spacer(Modifier.height(16.dp))

            // ─── Change password (parity with web) ──────────────────────────
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("SECURITY")
                Spacer(Modifier.height(12.dp))
                ZeninTextField(
                    value = currentPw,
                    onValueChange = { currentPw = it },
                    label = "CURRENT PASSWORD",
                    placeholder = "Enter current password",
                    singleLine = true,
                    isPassword = true,
                    leadingIcon = { Icon(Icons.Default.Lock, contentDescription = null, tint = CyanDim, modifier = Modifier.size(18.dp)) }
                )
                Spacer(Modifier.height(12.dp))
                ZeninTextField(
                    value = newPw,
                    onValueChange = { newPw = it },
                    label = "NEW PASSWORD",
                    placeholder = "8–64 characters, no spaces",
                    singleLine = true,
                    isPassword = true,
                    leadingIcon = { Icon(Icons.Default.Lock, contentDescription = null, tint = CyanDim, modifier = Modifier.size(18.dp)) }
                )
                if (pwMsg != null) {
                    Spacer(Modifier.height(10.dp))
                    Text(
                        pwMsg!!,
                        style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp,
                            color = if (pwMsg!!.startsWith("Password changed")) Online else Offline)
                    )
                }
                Spacer(Modifier.height(16.dp))
                ZeninButton(
                    text = if (pwBusy) "CHANGING…" else "CHANGE PASSWORD",
                    onClick = {
                        vm.changePassword(currentPw, newPw)
                        currentPw = ""
                        newPw = ""
                    },
                    modifier = Modifier.fillMaxWidth(),
                    enabled = !pwBusy && currentPw.isNotBlank() && newPw.isNotBlank()
                )
            }

            Spacer(Modifier.height(16.dp))

            // ─── Panels (parity with web Settings → Firebase panels) ────────
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("FIREBASE PANELS")
                Spacer(Modifier.height(8.dp))
                Text(
                    "Connect and manage the Firebase panels your devices report to.",
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurfaceDim)
                )
                Spacer(Modifier.height(16.dp))
                ZeninOutlinedButton(
                    text = "MANAGE PANELS →",
                    onClick = onPanelsClick,
                    modifier = Modifier.fillMaxWidth()
                )
            }

            Spacer(Modifier.height(16.dp))

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

            Spacer(Modifier.height(24.dp))
        }
    }
}
