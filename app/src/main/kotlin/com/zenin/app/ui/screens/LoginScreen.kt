package com.zenin.app.ui.screens

import androidx.compose.animation.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.zenin.app.data.SessionInfo
import com.zenin.app.ui.components.*
import com.zenin.app.ui.components.octagonShape
import com.zenin.app.ui.theme.*
import com.zenin.app.viewmodel.AuthUiState
import com.zenin.app.viewmodel.AuthViewModel
import kotlinx.coroutines.delay

@Composable
fun LoginScreen(
    onLoginSuccess: () -> Unit,
    vm: AuthViewModel = viewModel()
) {
    val uiState by vm.uiState.collectAsStateWithLifecycle()
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    val passwordFocus = remember { FocusRequester() }
    val focusManager = LocalFocusManager.current

    LaunchedEffect(uiState) {
        if (uiState is AuthUiState.Success) onLoginSuccess()
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(BgDark)
    ) {
        StarfieldBackground()

        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .imePadding()
                .padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            // ── Logo ─────────────────────────────────────────────────────────
            LogoSection()

            Spacer(Modifier.height(32.dp))

            // ── Main frame ───────────────────────────────────────────────────
            AnimatedContent(
                targetState = uiState,
                transitionSpec = {
                    slideInHorizontally { it } + fadeIn() togetherWith
                    slideOutHorizontally { -it } + fadeOut()
                },
                label = "loginstate"
            ) { state ->
                when (state) {
                    is AuthUiState.OtpPending -> OtpPanel(
                        otpId = state.otpId,
                        onVerify = { otp -> vm.verifyOtp(state.otpId, otp) },
                        onBack = { vm.resetState() },
                        loading = false
                    )
                    is AuthUiState.CapacityFull -> EvictionPanel(
                        sessions = state.sessions,
                        preAuthId = state.preAuthId,
                        onEvict = { sessionId -> vm.evictAndLogin(state.preAuthId, sessionId) },
                        onBack = { vm.resetState() }
                    )
                    is AuthUiState.Loading -> LoadingPanel()
                    else -> LoginPanel(
                        username = username,
                        onUsernameChange = { username = it },
                        password = password,
                        onPasswordChange = { password = it },
                        error = (state as? AuthUiState.Error)?.message,
                        passwordFocus = passwordFocus,
                        onSubmit = {
                            focusManager.clearFocus()
                            vm.login(username, password)
                        }
                    )
                }
            }

            Spacer(Modifier.height(24.dp))
        }
    }
}

@Composable
private fun LogoSection() {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        // Wing mark (drawn as stacked diamonds in Compose)
        Box(
            modifier = Modifier.size(72.dp),
            contentAlignment = Alignment.Center
        ) {
            // Outer glow ring
            Box(
                modifier = Modifier
                    .size(72.dp)
                    .clip(androidx.compose.foundation.shape.CircleShape)
                    .background(Cyan.copy(alpha = 0.08f))
                    .border(1.dp, Cyan.copy(alpha = 0.3f), androidx.compose.foundation.shape.CircleShape)
            )
            Text("⌂", style = TextStyle(fontSize = 32.sp, color = Cyan, fontWeight = FontWeight.Black))
        }
        Spacer(Modifier.height(12.dp))
        Text(
            text = "ZENIN",
            style = TextStyle(
                fontFamily = OrbitronFamily,
                fontWeight = FontWeight.Black,
                fontSize = 32.sp,
                letterSpacing = 10.sp,
                color = Cyan
            )
        )
        Spacer(Modifier.height(4.dp))
        Text(
            text = "DEVICE MONITORING",
            style = TextStyle(
                fontFamily = MonoFamily,
                fontSize = 11.sp,
                letterSpacing = 4.sp,
                color = CyanDim
            )
        )
    }
}

@Composable
private fun LoginPanel(
    username: String,
    onUsernameChange: (String) -> Unit,
    password: String,
    onPasswordChange: (String) -> Unit,
    error: String?,
    passwordFocus: FocusRequester,
    onSubmit: () -> Unit
) {
    val shape = octagonShape(16f)
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(Surface.copy(alpha = 0.95f))
            .border(1.dp, Brush.linearGradient(listOf(Cyan.copy(0.5f), CyanFaint, Cyan.copy(0.5f))), shape)
            .padding(28.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            "ACCESS TERMINAL",
            style = TextStyle(
                fontFamily = OrbitronFamily,
                fontWeight = FontWeight.Black,
                fontSize = 13.sp,
                letterSpacing = 4.sp,
                color = CyanDim
            )
        )
        Spacer(Modifier.height(20.dp))

        ZeninTextField(
            value = username,
            onValueChange = onUsernameChange,
            label = "USERNAME",
            placeholder = "Enter username",
            leadingIcon = { Icon(Icons.Default.Person, contentDescription = null, tint = CyanDim, modifier = Modifier.size(18.dp)) },
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
            keyboardActions = KeyboardActions(onNext = { passwordFocus.requestFocus() })
        )
        Spacer(Modifier.height(12.dp))

        ZeninTextField(
            value = password,
            onValueChange = onPasswordChange,
            label = "PASSWORD",
            placeholder = "Enter password",
            isPassword = true,
            leadingIcon = { Icon(Icons.Default.Lock, contentDescription = null, tint = CyanDim, modifier = Modifier.size(18.dp)) },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { onSubmit() }),
            modifier = Modifier.focusRequester(passwordFocus)
        )

        if (error != null) {
            Spacer(Modifier.height(12.dp))
            ErrorBanner(error)
        }

        Spacer(Modifier.height(24.dp))
        ZeninButton(
            text = "SIGN IN",
            onClick = onSubmit,
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(Modifier.height(12.dp))
        Text(
            text = "OTP will be sent to your Telegram",
            style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim),
            textAlign = TextAlign.Center
        )
    }
}

@Composable
private fun OtpPanel(otpId: String, onVerify: (String) -> Unit, onBack: () -> Unit, loading: Boolean) {
    var otp by remember { mutableStateOf("") }
    var secondsLeft by remember { mutableIntStateOf(120) }

    LaunchedEffect(Unit) {
        while (secondsLeft > 0) {
            delay(1000)
            secondsLeft--
        }
    }

    val shape = octagonShape(16f)
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(Surface.copy(alpha = 0.95f))
            .border(1.dp, Brush.linearGradient(listOf(Cyan.copy(0.5f), CyanFaint, Cyan.copy(0.5f))), shape)
            .padding(28.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text("TELEGRAM OTP", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Black, fontSize = 13.sp, letterSpacing = 4.sp, color = CyanDim))
        Spacer(Modifier.height(8.dp))
        Text(
            "A 6-digit code was sent to your Telegram via @ZeninPortalBot",
            style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurfaceDim),
            textAlign = TextAlign.Center
        )
        Spacer(Modifier.height(20.dp))

        // OTP digit boxes
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            repeat(6) { idx ->
                val char = otp.getOrNull(idx)?.toString() ?: ""
                Box(
                    modifier = Modifier
                        .size(44.dp, 52.dp)
                        .clip(RoundedCornerShape(6.dp))
                        .background(if (char.isNotEmpty()) Surface2 else Surface)
                        .border(1.dp, if (char.isNotEmpty()) Cyan else CyanFaint, RoundedCornerShape(6.dp)),
                    contentAlignment = Alignment.Center
                ) {
                    Text(char, style = TextStyle(fontFamily = MonoFamily, fontWeight = FontWeight.Bold, fontSize = 20.sp, color = Cyan))
                }
            }
        }

        Spacer(Modifier.height(12.dp))
        // Hidden text field for OTP input
        OutlinedTextField(
            value = otp,
            onValueChange = { v ->
                val digits = v.filter { it.isDigit() }.take(6)
                otp = digits
                if (digits.length == 6) onVerify(digits)
            },
            modifier = Modifier.size(1.dp),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
            colors = OutlinedTextFieldDefaults.colors(
                focusedBorderColor = Color.Transparent,
                unfocusedBorderColor = Color.Transparent
            )
        )

        // Timer
        Text(
            text = if (secondsLeft > 0) "Code expires in ${secondsLeft}s" else "Code may have expired",
            style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = if (secondsLeft > 30) OnSurfaceDim else Offline)
        )
        Spacer(Modifier.height(20.dp))

        ZeninButton(text = "VERIFY", onClick = { if (otp.length == 6) onVerify(otp) }, modifier = Modifier.fillMaxWidth(), loading = loading)
        Spacer(Modifier.height(8.dp))
        ZeninOutlinedButton(text = "BACK", onClick = onBack, modifier = Modifier.fillMaxWidth())
    }
}

@Composable
private fun EvictionPanel(sessions: List<SessionInfo>, preAuthId: String, onEvict: (Int) -> Unit, onBack: () -> Unit) {
    val shape = octagonShape(16f)
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(Surface.copy(alpha = 0.95f))
            .border(1.dp, Brush.linearGradient(listOf(Offline.copy(0.6f), CyanFaint, Cyan.copy(0.5f))), shape)
            .padding(24.dp)
    ) {
        Text("SESSION CAPACITY FULL", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Black, fontSize = 12.sp, letterSpacing = 3.sp, color = Warning))
        Spacer(Modifier.height(6.dp))
        Text("Max 2 active sessions. Revoke one to continue.", style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurfaceDim))
        Spacer(Modifier.height(16.dp))

        sessions.forEach { session ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(octagonShape(8f))
                    .background(Surface2)
                    .border(1.dp, CyanFaint, octagonShape(8f))
                    .padding(12.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(session.displayLocation, style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurface))
                    Text(session.occurredAt.take(16).replace("T", " "), style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp, color = OnSurfaceDim))
                }
                Spacer(Modifier.width(8.dp))
                ZeninOutlinedButton(text = "KICK", onClick = { onEvict(session.id) })
            }
            Spacer(Modifier.height(8.dp))
        }

        ZeninOutlinedButton(text = "BACK", onClick = onBack, modifier = Modifier.fillMaxWidth())
    }
}

@Composable
private fun LoadingPanel() {
    Box(
        modifier = Modifier.fillMaxWidth().height(200.dp),
        contentAlignment = Alignment.Center
    ) {
        CircularProgressIndicator(color = Cyan, strokeWidth = 2.dp)
    }
}

@Composable
private fun ErrorBanner(message: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(6.dp))
            .background(Offline.copy(alpha = 0.12f))
            .border(1.dp, Offline.copy(alpha = 0.4f), RoundedCornerShape(6.dp))
            .padding(12.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(Icons.Default.Warning, contentDescription = null, tint = Offline, modifier = Modifier.size(16.dp))
        Spacer(Modifier.width(8.dp))
        Text(message, style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = Offline))
    }
}
