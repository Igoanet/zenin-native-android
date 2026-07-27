package com.zenin.app.ui.components

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import kotlin.random.Random

private data class Star(
    val x: Float,   // 0..1 relative
    val y: Float,
    val size: Float,
    val alpha: Float,
    val speed: Float // twinkle phase offset
)

@Composable
fun StarfieldBackground(modifier: Modifier = Modifier) {
    val stars = remember {
        List(120) {
            Star(
                x = Random.nextFloat(),
                y = Random.nextFloat(),
                size = Random.nextFloat() * 1.8f + 0.4f,
                alpha = Random.nextFloat() * 0.6f + 0.15f,
                speed = Random.nextFloat()
            )
        }
    }

    val infiniteTransition = rememberInfiniteTransition(label = "stars")
    val time by infiniteTransition.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(8000, easing = LinearEasing),
            repeatMode = RepeatMode.Restart
        ),
        label = "startime"
    )

    Canvas(modifier = modifier.fillMaxSize()) {
        stars.forEach { star ->
            val phase = (time + star.speed) % 1f
            val twinkle = 0.5f + 0.5f * kotlin.math.sin(phase * 2 * kotlin.math.PI.toFloat())
            val alpha = star.alpha * (0.6f + 0.4f * twinkle)
            drawCircle(
                color = Color(0xFF00e8d8).copy(alpha = alpha * 0.7f),
                radius = star.size,
                center = Offset(star.x * size.width, star.y * size.height)
            )
        }
        // Subtle nebula blobs
        drawCircle(
            color = Color(0xFF00e8d8).copy(alpha = 0.03f),
            radius = size.minDimension * 0.6f,
            center = Offset(size.width * 0.15f, size.height * 0.3f)
        )
        drawCircle(
            color = Color(0xFF001830).copy(alpha = 0.5f),
            radius = size.minDimension * 0.5f,
            center = Offset(size.width * 0.85f, size.height * 0.7f)
        )
    }
}
