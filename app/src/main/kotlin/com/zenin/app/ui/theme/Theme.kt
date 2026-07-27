package com.zenin.app.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

private val ZeninColorScheme = darkColorScheme(
    primary = Cyan,
    onPrimary = BgDark,
    primaryContainer = Surface,
    onPrimaryContainer = Cyan,
    secondary = CyanDim,
    onSecondary = BgDark,
    background = BgDark,
    onBackground = OnSurface,
    surface = Surface,
    onSurface = OnSurface,
    surfaceVariant = Surface2,
    onSurfaceVariant = OnSurfaceDim,
    error = Error,
    onError = OnSurface,
    outline = CyanFaint,
)

@Composable
fun ZeninTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = ZeninColorScheme,
        typography = ZeninTypography,
        content = content
    )
}
