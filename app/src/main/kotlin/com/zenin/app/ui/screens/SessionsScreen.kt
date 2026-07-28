package com.zenin.app.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import com.zenin.app.data.SessionInfo
import com.zenin.app.ui.components.*
import com.zenin.app.ui.theme.*
import com.zenin.app.viewmodel.SessionsViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SessionsScreen(
    onBack: () -> Unit,
    vm: SessionsViewModel = viewModel()
) {
    val state by vm.state.collectAsStateWithLifecycle()

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text("ACTIVE SESSIONS", style = TextStyle(
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
                    IconButton(onClick = { vm.loadSessions() }) {
                        Icon(Icons.Default.Refresh, contentDescription = "Refresh", tint = CyanDim)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = BgDark)
            )
        },
        containerColor = BgDark
    ) { padding ->
        if (state.isLoading) {
            Box(modifier = Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = Cyan, strokeWidth = 2.dp)
            }
        } else if (state.error != null) {
            Box(modifier = Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Icon(Icons.Default.Error, contentDescription = null, tint = Offline, modifier = Modifier.size(40.dp))
                    Spacer(Modifier.height(8.dp))
                    Text(state.error!!, style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim))
                    Spacer(Modifier.height(16.dp))
                    ZeninButton("RETRY", onClick = { vm.loadSessions() })
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                item {
                    Text(
                        "${state.sessions.size} active session${if (state.sessions.size != 1) "s" else ""}",
                        style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurfaceDim)
                    )
                    Spacer(Modifier.height(4.dp))
                }

                if (state.sessions.size > 1) {
                    item {
                        ZeninOutlinedButton(
                            text = "TERMINATE ALL OTHER SESSIONS",
                            onClick = { vm.terminateOthers() },
                            modifier = Modifier.fillMaxWidth(),
                            enabled = state.revokingId == null
                        )
                        Spacer(Modifier.height(4.dp))
                    }
                }

                items(state.sessions, key = { it.id }) { session ->
                    SessionCard(
                        session = session,
                        isRevoking = state.revokingId == session.id,
                        onRevoke = { vm.revokeSession(session.id) }
                    )
                }

                if (state.sessions.isEmpty()) {
                    item {
                        Box(modifier = Modifier.fillMaxWidth().padding(40.dp), contentAlignment = Alignment.Center) {
                            Text("No active sessions", style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim))
                        }
                    }
                }

                item { Spacer(Modifier.height(16.dp)) }
            }
        }
    }
}

@Composable
private fun SessionCard(session: SessionInfo, isRevoking: Boolean, onRevoke: () -> Unit) {
    ZeninCard(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Icon(Icons.Default.PhoneAndroid, contentDescription = null, tint = CyanDim, modifier = Modifier.size(16.dp))
                    Text(
                        session.displayLocation,
                        style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurface, fontWeight = FontWeight.Medium)
                    )
                }
                Spacer(Modifier.height(4.dp))
                Text(
                    "IP: ${session.ip ?: "Unknown"}",
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim)
                )
                Text(
                    session.occurredAt.take(19).replace("T", "  "),
                    style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim)
                )
            }
            Spacer(Modifier.width(12.dp))
            if (isRevoking) {
                CircularProgressIndicator(modifier = Modifier.size(24.dp), color = Offline, strokeWidth = 2.dp)
            } else {
                ZeninOutlinedButton(
                    text = "REVOKE",
                    onClick = onRevoke,
                    enabled = true
                )
            }
        }
    }
}
