package com.zenin.app.nav

import androidx.compose.runtime.*
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.google.gson.Gson
import com.zenin.app.ZeninApp
import com.zenin.app.data.Device
import com.zenin.app.ui.screens.*
import com.zenin.app.viewmodel.AuthViewModel
import kotlinx.coroutines.flow.collectLatest

private const val ROUTE_LOGIN = "login"
private const val ROUTE_DASHBOARD = "dashboard"
private const val ROUTE_DEVICE = "device/{deviceJson}"
private const val ROUTE_SESSIONS = "sessions"
private const val ROUTE_SETTINGS = "settings"
private const val ROUTE_PANELS = "panels"

@Composable
fun AppNavigation() {
    val navController = rememberNavController()
    val authVm: AuthViewModel = viewModel()
    val token by ZeninApp.instance.authRepository.token.collectAsState()
    val gson = remember { Gson() }

    val startDest = if (token != null) ROUTE_DASHBOARD else ROUTE_LOGIN

    // If token is cleared at runtime (logout), force back to login
    LaunchedEffect(Unit) {
        ZeninApp.instance.authRepository.token.collectLatest { t ->
            if (t == null) {
                navController.navigate(ROUTE_LOGIN) {
                    popUpTo(0) { inclusive = true }
                }
            }
        }
    }

    NavHost(navController = navController, startDestination = startDest) {

        composable(ROUTE_LOGIN) {
            LoginScreen(
                onLoginSuccess = {
                    navController.navigate(ROUTE_DASHBOARD) {
                        popUpTo(ROUTE_LOGIN) { inclusive = true }
                    }
                },
                vm = authVm
            )
        }

        composable(ROUTE_DASHBOARD) {
            DashboardScreen(
                onDeviceClick = { device ->
                    val json = java.net.URLEncoder.encode(gson.toJson(device), "UTF-8")
                    navController.navigate("device/$json")
                },
                onSessionsClick = { navController.navigate(ROUTE_SESSIONS) },
                onSettingsClick = { navController.navigate(ROUTE_SETTINGS) },
                onLogout = {
                    authVm.logout()
                    // token flow handles navigation
                }
            )
        }

        composable(
            route = ROUTE_DEVICE,
            arguments = listOf(navArgument("deviceJson") { type = NavType.StringType })
        ) { backStackEntry ->
            val deviceJson = backStackEntry.arguments?.getString("deviceJson") ?: ""
            val device = runCatching {
                gson.fromJson(java.net.URLDecoder.decode(deviceJson, "UTF-8"), Device::class.java)
            }.getOrNull() ?: Device()

            DeviceDetailScreen(
                device = device,
                onBack = { navController.popBackStack() }
            )
        }

        composable(ROUTE_SESSIONS) {
            SessionsScreen(onBack = { navController.popBackStack() })
        }

        composable(ROUTE_SETTINGS) {
            SettingsScreen(
                onBack = { navController.popBackStack() },
                onPanelsClick = { navController.navigate(ROUTE_PANELS) }
            )
        }

        composable(ROUTE_PANELS) {
            PanelsScreen(onBack = { navController.popBackStack() })
        }
    }
}
