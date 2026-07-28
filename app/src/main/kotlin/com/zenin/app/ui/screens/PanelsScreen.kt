package com.zenin.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
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
import com.zenin.app.data.PanelConfigInfo
import com.zenin.app.ui.components.*
import com.zenin.app.ui.theme.*
import com.zenin.app.viewmodel.PanelsViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PanelsScreen(
    onBack: () -> Unit,
    vm: PanelsViewModel = viewModel()
) {
    val state by vm.state.collectAsStateWithLifecycle()
    var showAdd by remember { mutableStateOf(false) }
    var deleteTarget by remember { mutableStateOf<PanelConfigInfo?>(null) }

    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(state.actionMsg) {
        state.actionMsg?.let {
            snackbarHostState.showSnackbar(it)
            vm.clearMsg()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text("FIREBASE PANELS", style = TextStyle(
                        fontFamily = OrbitronFamily, fontWeight = FontWeight.Black,
                        fontSize = 15.sp, letterSpacing = 3.sp, color = Cyan
                    ))
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Default.ArrowBack, contentDescription = "Back", tint = CyanDim)
                    }
                },
                actions = {
                    IconButton(onClick = { showAdd = true }) {
                        Icon(Icons.Default.Add, contentDescription = "Add panel", tint = Cyan)
                    }
                    IconButton(onClick = { vm.load() }) {
                        Icon(Icons.Default.Refresh, contentDescription = "Refresh", tint = CyanDim)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = BgDark)
            )
        },
        snackbarHost = { SnackbarHost(snackbarHostState) },
        containerColor = BgDark
    ) { padding ->
        when {
            state.isLoading -> {
                Box(modifier = Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(color = Cyan, strokeWidth = 2.dp)
                }
            }
            state.error != null -> {
                Box(modifier = Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(Icons.Default.Error, contentDescription = null, tint = Offline, modifier = Modifier.size(40.dp))
                        Spacer(Modifier.height(8.dp))
                        Text(state.error!!, style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim))
                        Spacer(Modifier.height(16.dp))
                        ZeninButton("RETRY", onClick = { vm.load() })
                    }
                }
            }
            else -> {
                LazyColumn(
                    modifier = Modifier.fillMaxSize().padding(padding),
                    contentPadding = PaddingValues(16.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    item {
                        Text(
                            "${state.configs.size} panel${if (state.configs.size != 1) "s" else ""} connected",
                            style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurfaceDim)
                        )
                    }
                    items(state.configs, key = { it.id }) { config ->
                        PanelCard(
                            config = config,
                            busy = state.busyId == config.id,
                            onTest = { vm.test(config.id) },
                            onToggle = { vm.toggleActive(config) },
                            onDelete = { deleteTarget = config }
                        )
                    }
                    if (state.configs.isEmpty()) {
                        item {
                            Box(modifier = Modifier.fillMaxWidth().padding(40.dp), contentAlignment = Alignment.Center) {
                                Text("No panels yet — tap + to connect one",
                                    style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim))
                            }
                        }
                    }
                }
            }
        }
    }

    if (showAdd) {
        AddPanelDialog(
            onAdd = { name, url, secret -> vm.add(name, url, secret); showAdd = false },
            onDismiss = { showAdd = false }
        )
    }

    deleteTarget?.let { target ->
        AlertDialog(
            onDismissRequest = { deleteTarget = null },
            containerColor = Surface,
            title = { Text("Remove panel?", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold, fontSize = 15.sp, color = OnSurface)) },
            text = { Text("\"${target.name}\" and its connection will be removed. Devices on it will stop syncing.", style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim)) },
            confirmButton = {
                TextButton(onClick = { deleteTarget = null; vm.delete(target.id) }) {
                    Text("DELETE", style = TextStyle(fontFamily = MonoFamily, color = Offline, letterSpacing = 1.sp))
                }
            },
            dismissButton = {
                TextButton(onClick = { deleteTarget = null }) {
                    Text("CANCEL", style = TextStyle(fontFamily = MonoFamily, color = CyanDim, letterSpacing = 1.sp))
                }
            }
        )
    }
}

@Composable
private fun PanelCard(
    config: PanelConfigInfo,
    busy: Boolean,
    onTest: () -> Unit,
    onToggle: () -> Unit,
    onDelete: () -> Unit
) {
    ZeninCard(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(config.name, style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold,
                    fontSize = 14.sp, color = OnSurface))
                Spacer(Modifier.height(4.dp))
                Text(config.firebaseUrl, style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim))
            }
            StatusDot(online = config.isActive)
        }
        Spacer(Modifier.height(4.dp))
        Text(if (config.isActive) "ACTIVE" else "DISABLED",
            style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp, letterSpacing = 2.sp,
                color = if (config.isActive) Online else OnSurfaceDim))
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            ZeninOutlinedButton(text = "TEST", onClick = onTest, enabled = !busy)
            ZeninOutlinedButton(
                text = if (config.isActive) "DISABLE" else "ENABLE",
                onClick = onToggle,
                enabled = !busy
            )
            Spacer(Modifier.weight(1f))
            TextButton(onClick = onDelete, enabled = !busy) {
                Text("DELETE", style = TextStyle(fontFamily = MonoFamily, color = Offline, letterSpacing = 1.sp))
            }
        }
    }
}

@Composable
private fun AddPanelDialog(onAdd: (String, String, String) -> Unit, onDismiss: () -> Unit) {
    var name by remember { mutableStateOf("") }
    var url by remember { mutableStateOf("") }
    var secret by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Surface,
        title = { Text("Connect panel", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold, fontSize = 15.sp, color = OnSurface)) },
        text = {
            Column {
                ZeninTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = "PANEL NAME",
                    placeholder = "e.g. Main panel",
                    singleLine = true
                )
                Spacer(Modifier.height(12.dp))
                ZeninTextField(
                    value = url,
                    onValueChange = { url = it },
                    label = "FIREBASE URL",
                    placeholder = "https://xxx-default-rtdb.firebaseio.com",
                    singleLine = true
                )
                Spacer(Modifier.height(12.dp))
                ZeninTextField(
                    value = secret,
                    onValueChange = { secret = it },
                    label = "DATABASE SECRET",
                    placeholder = "Firebase database secret or JWT",
                    singleLine = true,
                    isPassword = true
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onAdd(name, url, secret) },
                enabled = name.isNotBlank() && url.isNotBlank() && secret.isNotBlank()
            ) {
                Text("CONNECT", style = TextStyle(fontFamily = MonoFamily, color = Cyan, letterSpacing = 1.sp))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("CANCEL", style = TextStyle(fontFamily = MonoFamily, color = CyanDim, letterSpacing = 1.sp))
            }
        }
    )
}
