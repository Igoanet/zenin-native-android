package com.zenin.app.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.GenericShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.graphics.*
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zenin.app.ui.theme.*

// ── Octagon shape helper ───────────────────────────────────────────────────────
fun octagonShape(cutPx: Float): Shape = GenericShape { size, _ ->
    val c = cutPx.coerceAtMost(size.minDimension / 2f)
    moveTo(c, 0f)
    lineTo(size.width - c, 0f)
    lineTo(size.width, c)
    lineTo(size.width, size.height - c)
    lineTo(size.width - c, size.height)
    lineTo(c, size.height)
    lineTo(0f, size.height - c)
    lineTo(0f, c)
    close()
}

// ── Glow draw modifier ────────────────────────────────────────────────────────
fun Modifier.cyanGlow(radius: Float = 20f, alpha: Float = 0.35f): Modifier = this.drawBehind {
    val nativePaint = android.graphics.Paint().apply {
        isAntiAlias = true
        color = android.graphics.Color.TRANSPARENT
        setShadowLayer(radius, 0f, 0f, android.graphics.Color.argb((alpha * 255).toInt(), 0, 232, 216))
    }
    drawContext.canvas.nativeCanvas.drawRect(
        0f, 0f, size.width, size.height, nativePaint
    )
}

// ── ZENIN Primary Button ──────────────────────────────────────────────────────
@Composable
fun ZeninButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    loading: Boolean = false,
    cutDp: Dp = 10.dp
) {
    val shape = octagonShape(with(androidx.compose.ui.platform.LocalDensity.current) { cutDp.toPx() })
    val bgColor = if (enabled) Cyan else Cyan.copy(alpha = 0.3f)

    Box(
        modifier = modifier
            .clip(shape)
            .background(bgColor)
            .then(if (enabled && !loading) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(horizontal = 32.dp, vertical = 14.dp),
        contentAlignment = Alignment.Center
    ) {
        if (loading) {
            CircularProgressIndicator(
                modifier = Modifier.size(20.dp),
                color = BgDark,
                strokeWidth = 2.dp
            )
        } else {
            Text(
                text = text,
                style = TextStyle(
                    fontFamily = OrbitronFamily,
                    fontWeight = FontWeight.Black,
                    fontSize = 13.sp,
                    letterSpacing = 3.sp,
                    color = BgDark
                )
            )
        }
    }
}

// ── ZENIN Outlined Button (secondary) ────────────────────────────────────────
@Composable
fun ZeninOutlinedButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    cutDp: Dp = 8.dp
) {
    val shape = octagonShape(with(androidx.compose.ui.platform.LocalDensity.current) { cutDp.toPx() })
    Box(
        modifier = modifier
            .clip(shape)
            .border(1.dp, if (enabled) CyanDim else CyanFaint, shape)
            .then(if (enabled) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(horizontal = 24.dp, vertical = 10.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text = text,
            style = TextStyle(
                fontFamily = OrbitronFamily,
                fontWeight = FontWeight.Bold,
                fontSize = 11.sp,
                letterSpacing = 2.sp,
                color = if (enabled) CyanDim else OnSurfaceDim
            )
        )
    }
}

// ── ZENIN Card ────────────────────────────────────────────────────────────────
@Composable
fun ZeninCard(
    modifier: Modifier = Modifier,
    cutDp: Dp = 12.dp,
    glowing: Boolean = false,
    onClick: (() -> Unit)? = null,
    content: @Composable ColumnScope.() -> Unit
) {
    val density = androidx.compose.ui.platform.LocalDensity.current
    val shape = octagonShape(with(density) { cutDp.toPx() })
    val borderColor = if (glowing) Online.copy(alpha = 0.6f) else CyanFaint

    Column(
        modifier = modifier
            .clip(shape)
            .background(Surface)
            .border(
                width = 1.dp,
                brush = if (glowing)
                    Brush.linearGradient(listOf(Online.copy(alpha = 0.8f), Online.copy(alpha = 0.2f)))
                else
                    Brush.linearGradient(listOf(CyanFaint, Color.Transparent, CyanFaint)),
                shape = shape
            )
            .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(16.dp),
        content = content
    )
}

// ── ZENIN TextField ───────────────────────────────────────────────────────────
@Composable
fun ZeninTextField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    modifier: Modifier = Modifier,
    placeholder: String = "",
    singleLine: Boolean = true,
    isPassword: Boolean = false,
    enabled: Boolean = true,
    leadingIcon: @Composable (() -> Unit)? = null,
    keyboardOptions: androidx.compose.foundation.text.KeyboardOptions = androidx.compose.foundation.text.KeyboardOptions.Default,
    keyboardActions: androidx.compose.foundation.text.KeyboardActions = androidx.compose.foundation.text.KeyboardActions.Default,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = {
            Text(
                text = label,
                style = TextStyle(
                    fontFamily = MonoFamily,
                    fontSize = 11.sp,
                    letterSpacing = 2.sp,
                    color = CyanDim
                )
            )
        },
        placeholder = {
            if (placeholder.isNotEmpty()) Text(
                placeholder,
                style = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp, color = OnSurfaceDim.copy(alpha = 0.5f))
            )
        },
        singleLine = singleLine,
        enabled = enabled,
        visualTransformation = if (isPassword)
            androidx.compose.ui.text.input.PasswordVisualTransformation()
        else
            androidx.compose.ui.text.input.VisualTransformation.None,
        leadingIcon = leadingIcon,
        colors = OutlinedTextFieldDefaults.colors(
            focusedBorderColor = Cyan,
            unfocusedBorderColor = CyanFaint,
            focusedLabelColor = Cyan,
            unfocusedLabelColor = OnSurfaceDim,
            cursorColor = Cyan,
            focusedTextColor = OnSurface,
            unfocusedTextColor = OnSurface,
            disabledBorderColor = CyanFaint.copy(alpha = 0.3f),
            focusedContainerColor = Surface2,
            unfocusedContainerColor = Surface,
        ),
        textStyle = TextStyle(fontFamily = MonoFamily, fontSize = 14.sp),
        keyboardOptions = keyboardOptions,
        keyboardActions = keyboardActions,
        modifier = modifier.fillMaxWidth()
    )
}

// ── Chip / tag ────────────────────────────────────────────────────────────────
@Composable
fun ZeninChip(text: String, color: Color = Cyan, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .clip(octagonShape(6f))
            .background(color.copy(alpha = 0.12f))
            .border(1.dp, color.copy(alpha = 0.4f), octagonShape(6f))
            .padding(horizontal = 10.dp, vertical = 4.dp)
    ) {
        Text(
            text = text,
            style = TextStyle(
                fontFamily = MonoFamily,
                fontSize = 10.sp,
                color = color,
                letterSpacing = 1.sp
            )
        )
    }
}

// ── Status dot ────────────────────────────────────────────────────────────────
@Composable
fun StatusDot(online: Boolean, modifier: Modifier = Modifier) {
    val color = if (online) Online else Offline
    Box(
        modifier = modifier
            .size(10.dp)
            .clip(androidx.compose.foundation.shape.CircleShape)
            .background(color)
            .then(if (online) Modifier.drawBehind {
                drawCircle(color.copy(alpha = 0.3f), radius = size.minDimension)
            } else Modifier)
    )
}

// ── Battery bar ───────────────────────────────────────────────────────────────
@Composable
fun BatteryBar(level: Int?, modifier: Modifier = Modifier) {
    val pct = (level ?: 0).coerceIn(0, 100)
    val color = when {
        pct > 60 -> Online
        pct > 20 -> Warning
        else -> Offline
    }
    Row(modifier = modifier, verticalAlignment = Alignment.CenterVertically) {
        Box(
            modifier = Modifier
                .width(40.dp)
                .height(7.dp)
                .clip(androidx.compose.foundation.shape.RoundedCornerShape(3.dp))
                .background(Surface2)
        ) {
            Box(
                modifier = Modifier
                    .fillMaxHeight()
                    .fillMaxWidth(pct / 100f)
                    .clip(androidx.compose.foundation.shape.RoundedCornerShape(3.dp))
                    .background(color)
            )
        }
        Spacer(Modifier.width(4.dp))
        Text(
            text = if (level != null) "$level%" else "—",
            style = TextStyle(fontFamily = MonoFamily, fontSize = 10.sp, color = color)
        )
    }
}

// ── Section header ────────────────────────────────────────────────────────────
@Composable
fun SectionHeader(title: String, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(
            text = title,
            style = MaterialTheme.typography.labelLarge.copy(color = CyanDim, letterSpacing = 3.sp),
        )
        Spacer(Modifier.width(8.dp))
        Divider(color = CyanFaint, thickness = 0.5.dp, modifier = Modifier.weight(1f))
    }
}

// ── Info row (label + value) ──────────────────────────────────────────────────
@Composable
fun InfoRow(label: String, value: String, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = 5.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.Top
    ) {
        Text(
            text = label,
            style = TextStyle(fontFamily = MonoFamily, fontSize = 11.sp, color = OnSurfaceDim),
            modifier = Modifier.weight(0.45f)
        )
        Text(
            text = value.ifBlank { "—" },
            style = TextStyle(fontFamily = MonoFamily, fontSize = 12.sp, color = OnSurface),
            modifier = Modifier.weight(0.55f)
        )
    }
}
