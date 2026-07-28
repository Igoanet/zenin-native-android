package com.zenin.app.ui.screens

import android.content.Intent
import android.net.Uri
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
import androidx.compose.material.icons.automirrored.filled.ArrowBack
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.zenin.app.data.SessionInfo
import com.zenin.app.ui.components.StarfieldBackground
import com.zenin.app.ui.components.octagonShape
import com.zenin.app.ui.theme.*
import com.zenin.app.viewmodel.AuthUiState
import com.zenin.app.viewmodel.AuthViewModel

private const val TELEGRAM_URL = "https://t.me/ZeninPortalBot"

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
                .padding(horizontal = 24.dp, vertical = 32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            // ── ZENIN wordmark ───────────────────────────────────────────────
            Text(
                text = "ZENIN",
                style = TextStyle(
                    fontFamily = OrbitronFamily,
                    fontWeight = FontWeight.Black,
                    fontSize = 46.sp,
                    letterSpacing = 12.sp,
                    color = Cyan
                )
            )

            Spacer(Modifier.height(36.dp))

            // ── Access terminal frame ────────────────────────────────────────
            LinkBoxFrame {
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
                            error = null,
                            onVerify = { otp -> vm.verifyOtp(state.otpId, otp) },
                            onBack = { vm.resetState() }
                        )
                        is AuthUiState.CapacityFull -> EvictionPanel(
                            sessions = state.sessions,
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
            }

            Spacer(Modifier.height(28.dp))

            ContactSupportButton()
        }
    }
}

// ── Octagon frame with corner accents (mirrors web LinkBoxFrame) ────────────
@Composable
private fun LinkBoxFrame(content: @Composable () -> Unit) {
    val shape = octagonShape(with(androidx.compose.ui.platform.LocalDensity.current) { 20.dp.toPx() })
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(
                Brush.verticalGradient(
                    listOf(Color(0xF20A1023), Color(0xFA020A19))
                )
            )
            .border(
                width = 1.dp,
                brush = Brush.linearGradient(
                    listOf(Cyan.copy(0.6f), Cyan.copy(0.22f), Cyan.copy(0.6f))
                ),
                shape = shape
            )
            .padding(horizontal = 28.dp, vertical = 30.dp)
    ) {
        content()
    }
}

// ── Field with icon+label above the input (mirrors web InputField) ──────────
@Composable
private fun ZeninLoginField(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    label: String,
    value: String,
    onChange: (String) -> Unit,
    placeholder: String,
    isPassword: Boolean = false,
    focusRequester: FocusRequester? = null,
    keyboardOptions: KeyboardOptions = KeyboardOptions.Default,
    keyboardActions: KeyboardActions = KeyboardActions.Default
) {
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(icon, contentDescription = null, tint = Cyan, modifier = Modifier.size(15.dp))
            Spacer(Modifier.width(6.dp))
            Text(
                label,
                style = TextStyle(fontFamily = MonoFamily, fontWeight = FontWeight.Bold,
                    fontSize = 11.sp, letterSpacing = 3.sp, color = Cyan)
            )
        }
        Spacer(Modifier.height(6.dp))
        OutlinedTextField(
            value = value,
            onValueChange = onChange,
            placeholder = {
                Text(placeholder, style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp,
                    color = OnSurfaceDim.copy(alpha = 0.5f)))
            },
            singleLine = true,
            visualTransformation = if (isPassword) PasswordVisualTransformation() else VisualTransformation.None,
            colors = OutlinedTextFieldDefaults.colors(
                focusedBorderColor = Cyan.copy(0.6f),
                unfocusedBorderColor = Color(0x4000B4C8),
                cursorColor = Cyan,
                focusedTextColor = Cyan,
                unfocusedTextColor = Cyan.copy(0.9f),
                focusedContainerColor = Color(0x99002850),
                unfocusedContainerColor = Color(0x80001E3C)
            ),
            textStyle = TextStyle(fontFamily = MonoFamily, fontSize = 14.sp, letterSpacing = 0.5.sp),
            keyboardOptions = keyboardOptions,
            keyboardActions = keyboardActions,
            shape = RoundedCornerShape(2.dp),
            modifier = Modifier
                .fillMaxWidth()
                .then(if (focusRequester != null) Modifier.focusRequester(focusRequester) else Modifier)
        )
    }
}

@Composable
private fun GlowButton(
    text: String,
    onClick: () -> Unit,
    enabled: Boolean = true,
    color: Color = Cyan,
    textColor: Color = Color(0xFF001520),
    modifier: Modifier = Modifier
) {
    val shape = octagonShape(with(androidx.compose.ui.platform.LocalDensity.current) { 8.dp.toPx() })
    Box(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(if (enabled) color else Color(0x2694A3B8))
            .then(if (enabled) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(vertical = 14.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text,
            style = TextStyle(
                fontFamily = OrbitronFamily, fontWeight = FontWeight.Black, fontSize = 14.sp,
                letterSpacing = 5.sp,
                color = if (enabled) textColor else Color(0x6694A3B8)
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
    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            "ACCESS TERMINAL",
            style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Black,
                fontSize = 18.sp, letterSpacing = 3.sp, color = CyanDim)
        )
        Spacer(Modifier.height(18.dp))

        ZeninLoginField(
            icon = Icons.Default.Person,
            label = "USER ID",
            value = username,
            onChange = onUsernameChange,
            placeholder = "Enter your user ID",
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
            keyboardActions = KeyboardActions(onNext = { passwordFocus.requestFocus() })
        )
        Spacer(Modifier.height(12.dp))
        ZeninLoginField(
            icon = Icons.Default.Lock,
            label = "PASSWORD",
            value = password,
            onChange = onPasswordChange,
            placeholder = "Enter password",
            isPassword = true,
            focusRequester = passwordFocus,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { onSubmit() })
        )

        if (error != null) {
            Spacer(Modifier.height(14.dp))
            ErrorBanner(error)
        }

        Spacer(Modifier.height(22.dp))
        GlowButton(text = "SIGN IN", onClick = onSubmit)

        Spacer(Modifier.height(16.dp))
        Text(
            "Get your credentials from @ZeninPortalBot",
            style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp, letterSpacing = 1.sp, color = CyanDim),
            textAlign = TextAlign.Center
        )
    }
}

@Composable
private fun OtpPanel(error: String?, onVerify: (String) -> Unit, onBack: () -> Unit) {
    var otp by remember { mutableStateOf("") }
    val focusRequester = remember { FocusRequester() }

    LaunchedEffect(Unit) { focusRequester.requestFocus() }

    Column(modifier = Modifier.fillMaxWidth()) {
        BackRow(onBack)
        Spacer(Modifier.height(14.dp))
        Column(
            modifier = Modifier.fillMaxWidth(),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Box(
                modifier = Modifier
                    .size(48.dp)
                    .clip(androidx.compose.foundation.shape.CircleShape)
                    .background(Cyan.copy(0.1f))
                    .border(1.dp, Cyan.copy(0.35f), androidx.compose.foundation.shape.CircleShape),
                contentAlignment = Alignment.Center
            ) {
                Icon(Icons.Default.Send, contentDescription = null, tint = Cyan, modifier = Modifier.size(20.dp))
            }
            Spacer(Modifier.height(12.dp))
            Text("CHECK TELEGRAM", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold,
                fontSize = 13.sp, letterSpacing = 3.sp, color = CyanDim))
            Spacer(Modifier.height(4.dp))
            if (otp.length < 6) {
                Text("A 6-digit code was sent to your Telegram account.",
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp, color = OnSurfaceDim),
                    textAlign = TextAlign.Center)
            }
            Spacer(Modifier.height(18.dp))

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                repeat(6) { idx ->
                    val char = otp.getOrNull(idx)?.toString() ?: ""
                    val isCurrent = idx == otp.length
                    Box(
                        modifier = Modifier
                            .size(42.dp, 52.dp)
                            .clip(RoundedCornerShape(4.dp))
                            .background(if (char.isNotEmpty()) Color(0xB3003259) else Color(0x80001E3C))
                            .border(
                                width = if (isCurrent) 2.dp else 1.dp,
                                color = when {
                                    char.isNotEmpty() -> Cyan.copy(0.6f)
                                    isCurrent -> Cyan.copy(0.5f)
                                    else -> Color(0x4000B4C8)
                                },
                                shape = RoundedCornerShape(4.dp)
                            )
                            .clickable { focusRequester.requestFocus() },
                        contentAlignment = Alignment.Center
                    ) {
                        Text(char, style = TextStyle(fontFamily = MonoFamily, fontWeight = FontWeight.Bold,
                            fontSize = 20.sp, color = Cyan))
                    }
                }
            }

            // Hidden field to drive keyboard input
            OutlinedTextField(
                value = otp,
                onValueChange = { v ->
                    val digits = v.filter { it.isDigit() }.take(6)
                    otp = digits
                    if (digits.length == 6) onVerify(digits)
                },
                modifier = Modifier.size(1.dp).focusRequester(focusRequester),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { if (otp.length == 6) onVerify(otp) }),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = Color.Transparent,
                    unfocusedBorderColor = Color.Transparent,
                    cursorColor = Color.Transparent
                )
            )

            if (error != null) {
                Spacer(Modifier.height(14.dp))
                ErrorBanner(error)
            }

            Spacer(Modifier.height(28.dp))
            GlowButton(text = "VERIFY", onClick = { if (otp.length == 6) onVerify(otp) }, enabled = otp.length == 6)
        }
    }
}

@Composable
private fun EvictionPanel(sessions: List<SessionInfo>, onEvict: (Int) -> Unit, onBack: () -> Unit) {
    var selectedId by remember { mutableStateOf<Int?>(null) }
    Column(modifier = Modifier.fillMaxWidth()) {
        BackRow(onBack)
        Spacer(Modifier.height(12.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Default.Warning, contentDescription = null, tint = Warning, modifier = Modifier.size(14.dp))
            Spacer(Modifier.width(8.dp))
            Text("LOGIN LIMIT REACHED", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold,
                fontSize = 12.sp, letterSpacing = 2.sp, color = Warning))
        }
        Spacer(Modifier.height(10.dp))
        Text(
            "Your account has 2 active sessions. Select one device to log out so you can sign in here.",
            style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp, color = CyanDim)
        )
        Spacer(Modifier.height(14.dp))
        sessions.take(2).forEach { s ->
            val selected = selectedId == s.id
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(6.dp))
                    .background(if (selected) Color(0x1AF87171) else Color(0x80001E3C))
                    .border(1.dp, if (selected) Offline.copy(0.5f) else CyanFaint, RoundedCornerShape(6.dp))
                    .clickable { selectedId = if (selected) null else s.id }
                    .padding(12.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.Computer, contentDescription = null,
                            tint = if (selected) Offline else CyanDim, modifier = Modifier.size(12.dp))
                        Spacer(Modifier.width(6.dp))
                        Text(s.displayLocation, style = TextStyle(fontFamily = MonoFamily, fontWeight = FontWeight.SemiBold,
                            fontSize = 11.sp, color = if (selected) Offline else CyanDim))
                    }
                    Text(
                        if (selected) "WILL LOGOUT" else "SELECT",
                        style = TextStyle(fontFamily = MonoFamily, fontWeight = FontWeight.Bold, fontSize = 8.sp,
                            letterSpacing = 1.sp, color = if (selected) Offline else CyanDim)
                    )
                }
                Spacer(Modifier.height(4.dp))
                Text(s.occurredAt.take(16).replace("T", " "),
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 9.sp, color = OnSurfaceDim))
            }
            Spacer(Modifier.height(8.dp))
        }
        Spacer(Modifier.height(6.dp))
        GlowButton(
            text = "CONTINUE →",
            onClick = { selectedId?.let(onEvict) },
            enabled = selectedId != null,
            color = Offline,
            textColor = Color(0xFF1A0000)
        )
    }
}

@Composable
private fun BackRow(onBack: () -> Unit) {
    Row(
        modifier = Modifier.clickable(onClick = onBack),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = null, tint = OnSurfaceDim, modifier = Modifier.size(12.dp))
        Spacer(Modifier.width(4.dp))
        Text("Back", style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp, color = OnSurfaceDim))
    }
}

@Composable
private fun ContactSupportButton() {
    val context = LocalContext.current
    val shape = octagonShape(with(androidx.compose.ui.platform.LocalDensity.current) { 10.dp.toPx() })
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(Color(0xE6030F23))
            .border(1.dp, Cyan.copy(0.35f), shape)
            .clickable {
                runCatching {
                    context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(TELEGRAM_URL)))
                }
            }
            .padding(vertical = 16.dp),
        contentAlignment = Alignment.Center
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Default.ChatBubble, contentDescription = null, tint = Cyan.copy(0.85f), modifier = Modifier.size(20.dp))
            Spacer(Modifier.width(10.dp))
            Text(
                "CONTACT SUPPORT",
                style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold, fontSize = 13.sp,
                    letterSpacing = 3.sp, color = Cyan.copy(0.85f))
            )
        }
    }
}

@Composable
private fun LoadingPanel() {
    Box(
        modifier = Modifier.fillMaxWidth().height(200.dp),
        contentAlignment = Alignment.Center
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            CircularProgressIndicator(color = Cyan, strokeWidth = 2.dp)
            Spacer(Modifier.height(16.dp))
            Text("AUTHENTICATING...", style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp,
                letterSpacing = 3.sp, color = OnSurfaceDim))
        }
    }
}

@Composable
private fun ErrorBanner(message: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(4.dp))
            .background(Offline.copy(alpha = 0.08f))
            .border(1.dp, Offline.copy(alpha = 0.3f), RoundedCornerShape(4.dp))
            .padding(12.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(Icons.Default.Warning, contentDescription = null, tint = Offline, modifier = Modifier.size(15.dp))
        Spacer(Modifier.width(8.dp))
        Text(message, style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = Offline))
    }
}
