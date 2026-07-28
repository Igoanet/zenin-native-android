package com.zenin.app.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.ui.draw.clip
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.automirrored.filled.Logout
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
import com.zenin.app.data.Device
import com.zenin.app.data.MoneyPoolSummary
import com.zenin.app.data.formatRupee
import com.zenin.app.ui.components.*
import com.zenin.app.ui.theme.*
import com.zenin.app.viewmodel.DevicesViewModel

private enum class DeviceFilter(val label: String) {
    ALL("ALL"), ONLINE("ONLINE"), BANK("BANK"), WALLET("WALLET"), CARDS("CARDS")
}

private fun Device.matches(f: DeviceFilter): Boolean = when (f) {
    DeviceFilter.ALL -> true
    DeviceFilter.ONLINE -> status
    DeviceFilter.BANK -> smsAnalysis?.hasBank == true
    DeviceFilter.WALLET -> smsAnalysis?.hasWallet == true
    DeviceFilter.CARDS -> smsAnalysis?.hasCards == true
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DashboardScreen(
    onDeviceClick: (Device) -> Unit,
    onSessionsClick: () -> Unit,
    onSettingsClick: () -> Unit,
    onLogout: () -> Unit,
    vm: DevicesViewModel = viewModel()
) {
    val state by vm.state.collectAsStateWithLifecycle()
    var filter by remember { mutableStateOf(DeviceFilter.ALL) }
    val filteredDevices = remember(state.devices, filter) { state.devices.filter { it.matches(filter) } }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text(
                        "ZENIN",
                        style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Black,
                            fontSize = 18.sp, letterSpacing = 4.sp, color = Cyan)
                    )
                },
                actions = {
                    IconButton(onClick = { vm.loadDevices() }) {
                        if (state.isLoading)
                            CircularProgressIndicator(modifier = Modifier.size(20.dp), color = CyanDim, strokeWidth = 2.dp)
                        else
                            Icon(Icons.Default.Refresh, contentDescription = "Refresh", tint = CyanDim)
                    }
                    IconButton(onClick = onSessionsClick) {
                        Icon(Icons.Default.Group, contentDescription = "Sessions", tint = CyanDim)
                    }
                    IconButton(onClick = onSettingsClick) {
                        Icon(Icons.Default.Settings, contentDescription = "Settings", tint = CyanDim)
                    }
                    IconButton(onClick = onLogout) {
                        Icon(Icons.AutoMirrored.Filled.Logout, contentDescription = "Logout", tint = OnSurfaceDim)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = BgDark,
                    scrolledContainerColor = Surface
                )
            )
        },
        containerColor = BgDark
    ) { padding ->
        if (state.error != null && state.devices.isEmpty()) {
            ErrorState(state.error!!, onRetry = { vm.loadDevices() }, modifier = Modifier.padding(padding))
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                item {
                    MoneyPoolHeader(summary = state.summary)
                }
                item {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        SectionHeader("DEVICES", modifier = Modifier.weight(1f))
                        Text(
                            "${state.devices.count { it.status }} online / ${state.devices.size} total",
                            style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim)
                        )
                    }
                }
                item {
                    FilterChipsRow(
                        selected = filter,
                        counts = mapOf(
                            DeviceFilter.ALL to state.devices.size,
                            DeviceFilter.ONLINE to state.devices.count { it.matches(DeviceFilter.ONLINE) },
                            DeviceFilter.BANK to state.devices.count { it.matches(DeviceFilter.BANK) },
                            DeviceFilter.WALLET to state.devices.count { it.matches(DeviceFilter.WALLET) },
                            DeviceFilter.CARDS to state.devices.count { it.matches(DeviceFilter.CARDS) }
                        ),
                        onSelect = { filter = it }
                    )
                }
                items(filteredDevices, key = { "${it.panelId}/${it.id}" }) { device ->
                    DeviceCard(device = device, onClick = { onDeviceClick(device) })
                }
                if (filteredDevices.isEmpty() && !state.isLoading) {
                    item {
                        Box(modifier = Modifier.fillMaxWidth().padding(40.dp), contentAlignment = Alignment.Center) {
                            Text(
                                if (state.devices.isEmpty()) "No devices found" else "No devices match this filter",
                                style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim)
                            )
                        }
                    }
                }
                item { Spacer(Modifier.height(16.dp)) }
            }
        }
    }
}

@Composable
private fun FilterChipsRow(
    selected: DeviceFilter,
    counts: Map<DeviceFilter, Int>,
    onSelect: (DeviceFilter) -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        DeviceFilter.entries.forEach { f ->
            val isSel = f == selected
            val count = counts[f] ?: 0
            Box(
                modifier = Modifier
                    .clip(octagonShape(6f))
                    .background(if (isSel) Cyan.copy(alpha = 0.18f) else Surface)
                    .border(1.dp, if (isSel) Cyan else CyanFaint, octagonShape(6f))
                    .clickable { onSelect(f) }
                    .padding(horizontal = 12.dp, vertical = 7.dp)
            ) {
                Text(
                    "${f.label} $count",
                    style = TextStyle(
                        fontFamily = MonoFamily,
                        fontSize = 11.sp,
                        letterSpacing = 1.sp,
                        color = if (isSel) Cyan else OnSurfaceDim
                    )
                )
            }
        }
    }
}

@Composable
private fun MoneyPoolHeader(summary: MoneyPoolSummary) {
    ZeninCard(modifier = Modifier.fillMaxWidth(), glowing = false) {
        Text("MONEY POOL", style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold,
            fontSize = 11.sp, letterSpacing = 3.sp, color = CyanDim))
        Spacer(Modifier.height(8.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column {
                Text("TOTAL", style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp, color = OnSurfaceDim, letterSpacing = 2.sp))
                Text(
                    formatRupee(summary.totalBalance),
                    style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Black, fontSize = 22.sp, color = Cyan)
                )
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ZeninChip("${summary.fundCount} FUNDED", color = Online)
                if (summary.unknownCount > 0) ZeninChip("${summary.unknownCount} UNKNOWN", color = Warning)
            }
        }
    }
}

@Composable
private fun DeviceCard(device: Device, onClick: () -> Unit) {
    ZeninCard(modifier = Modifier.fillMaxWidth(), glowing = device.status, onClick = onClick) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusDot(device.status)
                Spacer(Modifier.width(8.dp))
                Column {
                    Text(device.name, style = TextStyle(fontFamily = OrbitronFamily, fontWeight = FontWeight.Bold,
                        fontSize = 13.sp, color = OnSurface, letterSpacing = 0.5.sp))
                    Text(device.mobNo.ifBlank { device.serviceProvider },
                        style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim))
                }
            }
            BatteryBar(device.battery)
        }

        Spacer(Modifier.height(8.dp))
        HorizontalDivider(color = CyanFaint.copy(alpha = 0.3f), thickness = 0.5.dp)
        Spacer(Modifier.height(8.dp))

        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            Column {
                Text("LAST SEEN", style = TextStyle(fontFamily = MonoFamily, fontSize = 9.sp, color = OnSurfaceDim, letterSpacing = 1.sp))
                Text(formatRelativeTime(device.lastSeen), style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurface))
            }
            device.smsAnalysis?.let { analysis ->
                if (analysis.deviceTotal > 0) {
                    Column {
                        Text("BALANCE", style = TextStyle(fontFamily = MonoFamily, fontSize = 9.sp, color = OnSurfaceDim, letterSpacing = 1.sp))
                        Text(formatRupee(analysis.deviceTotal), style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = Cyan))
                    }
                }
            }
            Column {
                Text("ANDROID", style = TextStyle(fontFamily = MonoFamily, fontSize = 9.sp, color = OnSurfaceDim, letterSpacing = 1.sp))
                Text(device.androidV.take(6), style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurface))
            }
        }
    }
}

@Composable
private fun ErrorState(message: String, onRetry: () -> Unit, modifier: Modifier = Modifier) {
    Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(Icons.Default.Error, contentDescription = null, tint = Offline, modifier = Modifier.size(40.dp))
            Spacer(Modifier.height(8.dp))
            Text(message, style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim))
            Spacer(Modifier.height(16.dp))
            ZeninButton("RETRY", onClick = onRetry)
        }
    }
}

private fun formatRelativeTime(ts: Long): String {
    if (ts == 0L) return "—"
    val ms = if (ts < 1_000_000_000_000L) ts * 1000L else ts
    val diff = System.currentTimeMillis() - ms
    return when {
        diff < 60_000 -> "Just now"
        diff < 3_600_000 -> "${diff / 60_000}m ago"
        diff < 86_400_000 -> "${diff / 3_600_000}h ago"
        else -> "${diff / 86_400_000}d ago"
    }
}
