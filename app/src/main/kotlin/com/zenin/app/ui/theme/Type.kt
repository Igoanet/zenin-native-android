package com.zenin.app.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.googlefonts.Font
import androidx.compose.ui.text.googlefonts.GoogleFont
import androidx.compose.ui.unit.sp
import com.zenin.app.R

val provider = GoogleFont.Provider(
    providerAuthority = "com.google.android.gms.fonts",
    providerPackage = "com.google.android.gms",
    certificates = R.array.com_google_android_gms_fonts_certs
)

val OrbitronFamily = FontFamily(
    Font(googleFont = GoogleFont("Orbitron"), fontProvider = provider, weight = FontWeight.Normal),
    Font(googleFont = GoogleFont("Orbitron"), fontProvider = provider, weight = FontWeight.Bold),
    Font(googleFont = GoogleFont("Orbitron"), fontProvider = provider, weight = FontWeight.Black),
)

val MonoFamily = FontFamily(
    Font(googleFont = GoogleFont("JetBrains Mono"), fontProvider = provider, weight = FontWeight.Normal),
    Font(googleFont = GoogleFont("JetBrains Mono"), fontProvider = provider, weight = FontWeight.Medium),
    Font(googleFont = GoogleFont("JetBrains Mono"), fontProvider = provider, weight = FontWeight.Bold),
)

val ZeninTypography = Typography(
    displayLarge = TextStyle(
        fontFamily = OrbitronFamily,
        fontWeight = FontWeight.Black,
        fontSize = 28.sp,
        color = Cyan,
        letterSpacing = 4.sp
    ),
    displayMedium = TextStyle(
        fontFamily = OrbitronFamily,
        fontWeight = FontWeight.Black,
        fontSize = 22.sp,
        color = Cyan,
        letterSpacing = 3.sp
    ),
    headlineLarge = TextStyle(
        fontFamily = OrbitronFamily,
        fontWeight = FontWeight.Bold,
        fontSize = 18.sp,
        color = Cyan,
        letterSpacing = 2.sp
    ),
    headlineMedium = TextStyle(
        fontFamily = OrbitronFamily,
        fontWeight = FontWeight.Bold,
        fontSize = 15.sp,
        color = Cyan,
        letterSpacing = 1.5.sp
    ),
    titleLarge = TextStyle(
        fontFamily = OrbitronFamily,
        fontWeight = FontWeight.Bold,
        fontSize = 14.sp,
        color = OnSurface,
        letterSpacing = 1.sp
    ),
    bodyLarge = TextStyle(
        fontFamily = MonoFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 14.sp,
        color = OnSurface,
        letterSpacing = 0.sp
    ),
    bodyMedium = TextStyle(
        fontFamily = MonoFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 12.sp,
        color = OnSurfaceDim,
        letterSpacing = 0.sp
    ),
    labelLarge = TextStyle(
        fontFamily = OrbitronFamily,
        fontWeight = FontWeight.Bold,
        fontSize = 12.sp,
        color = OnSurface,
        letterSpacing = 2.sp
    ),
    labelMedium = TextStyle(
        fontFamily = MonoFamily,
        fontWeight = FontWeight.Normal,
        fontSize = 11.sp,
        color = OnSurfaceDim,
        letterSpacing = 0.5.sp
    ),
)
