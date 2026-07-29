"""AI Text plugin — generates and displays text from OpenAI or Google Gemini models."""

from plugins.base_plugin.base_plugin import BasePlugin
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from utils.app_utils import get_font
from utils.text_utils import draw_multiline_text, measure_text_block, get_text_dimensions
import logging
import os
import random

logger = logging.getLogger(__name__)

# System CJK font candidates (tried in order). The bundled Jost font only
# covers Latin glyphs, so AI responses in Chinese/Japanese/Korean would
# render as blank .notdef boxes. We fall back to the first available system
# CJK font when the content contains CJK characters.
#   macOS:    PingFang / STHeiti / Arial Unicode
#   RPi/Linux: Noto CJK / WenQuanYi / Droid Sans Fallback (preinstalled on RPi OS)
CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
]


def _contains_cjk(text):
    """Return True if text contains any CJK (Han/Hiragana/Katakana/Hangul) character."""
    for ch in text:
        cp = ord(ch)
        # CJK Unified Ideographs + extensions, Hiragana, Katakana,
        # Hangul Syllables, CJK Compatibility Ideographs
        if (0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF
                or 0xAC00 <= cp <= 0xD7AF or 0x3400 <= cp <= 0x4DBF
                or 0xF900 <= cp <= 0xFAFF or 0xFF00 <= cp <= 0xFFEF):
            return True
    return False


def _get_font_for_text(text, font_name, font_size, font_weight="normal"):
    """Return a font that can render `text`.

    Uses the bundled Jost (or other requested) font for Latin-only text.
    When `text` contains CJK characters, tries to load a system CJK font so
    glyphs render instead of blank boxes; falls back to the bundled font
    (with a warning) if no CJK font is installed.
    """
    bundled = get_font(font_name, font_size, font_weight)
    if not _contains_cjk(text):
        return bundled
    for path in CJK_FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, font_size)
            except Exception as e:
                logger.debug(f"Failed to load CJK font {path}: {e}")
    logger.warning(
        "Content contains CJK characters but no system CJK font was found. "
        f"Install fonts-noto-cjk (or similar). Tried: {CJK_FONT_CANDIDATES}"
    )
    return bundled

# OpenAI models
OPENAI_TEXT_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-5", "gpt-5-mini", "gpt-5-nano"]
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# Gemini models (use full model names for new API)
GEMINI_TEXT_MODELS = ["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-flash"]
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"

# DeepSeek models (OpenAI-compatible API at https://api.deepseek.com)
DEEPSEEK_TEXT_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"]
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class AIText(BasePlugin):
    """Sends a prompt to an AI model and renders the generated text on the display."""

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['api_key'] = {
            "required": False,
            "service": "OpenAI, Google Gemini, or DeepSeek",
            "expected_key": "OPEN_AI_SECRET, GOOGLE_GEMINI_SECRET, or DEEPSEEK_SECRET"
        }
        template_params['style_settings'] = True
        return template_params

    def generate_image(self, settings, device_config):
        """Generate text via the configured AI provider and render it on the display."""
        logger.info("=== AI Text Plugin: Starting text generation ===")

        provider = settings.get("provider", "openai")
        text_prompt = settings.get('textPrompt', '')

        # Use provided title, or auto-generate from prompt
        title = settings.get("title") or ""
        if not title and text_prompt:
            # Create title from prompt (truncated)
            title = text_prompt.strip()
            if len(title) > 40:
                title = title[:37] + "..."

        if not text_prompt.strip():
            raise RuntimeError("Text Prompt is required.")

        logger.info(f"Provider: {provider}")
        logger.debug(f"Prompt: '{text_prompt}'")

        if provider == "gemini":
            prompt_response = self._generate_with_gemini(settings, device_config, text_prompt)
        elif provider == "deepseek":
            prompt_response = self._generate_with_deepseek(settings, device_config, text_prompt)
        else:
            prompt_response = self._generate_with_openai(settings, device_config, text_prompt)

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        # Convert literal \n to actual newlines for PIL rendering
        formatted_response = prompt_response.replace('\\n', '\n')

        image = self._render_pil(dimensions, title, formatted_response, settings)

        logger.info("=== AI Text Plugin: Text generation complete ===")
        return image

    def _render_pil(self, dimensions, title, content, settings):
        width, height = dimensions
        bg_color = settings.get("backgroundColor", "#ffffff")
        text_color = settings.get("textColor", "#000000")

        margin = int(width * 0.05)
        content_width = width - margin * 2

        image = Image.new("RGBA", dimensions, bg_color)
        draw = ImageDraw.Draw(image)

        # Scale fonts relative to screen size
        title_size = int(min(height * 0.08, width * 0.07))
        content_size = int(min(height * 0.055, width * 0.045))

        title_font = _get_font_for_text(title, "Jost", title_size, "bold")
        content_font = _get_font_for_text(content, "Jost", content_size)

        y_cursor = margin

        # Draw title
        if title:
            tw = get_text_dimensions(draw, title.upper(), title_font)[0]
            title_visual_h = int(title_size * 1.15)
            tx = (width - tw) // 2
            draw.text((tx, y_cursor), title.upper(), font=title_font, fill=text_color)
            y_cursor += title_visual_h + int(height * 0.015)
            # Title underline
            line_x0 = (width - tw) // 2
            line_x1 = line_x0 + tw
            draw.line((line_x0, y_cursor, line_x1, y_cursor), fill=text_color, width=2)
            y_cursor += int(height * 0.03)

        # Draw quoted content, centered
        available_height = height - y_cursor - margin
        quoted = f'"{content}"'

        # Measure to vertically center content in remaining space
        block_height = measure_text_block(draw, quoted, content_font, content_width, line_spacing=int(content_size * 0.5))
        if block_height < available_height:
            y_cursor += (available_height - block_height) // 2

        draw_multiline_text(draw, quoted, (margin, y_cursor), content_font, text_color,
                            content_width, line_spacing=int(content_size * 0.5), align="center")

        return image

    def _generate_with_openai(self, settings, device_config, text_prompt):
        """Generate text using OpenAI."""
        from openai import OpenAI

        api_key = device_config.load_env_key("OPEN_AI_SECRET")
        if not api_key:
            raise RuntimeError("OpenAI API Key not configured. Add OPEN_AI_SECRET in Settings > API Keys.")

        # Sanitize API key
        api_key = api_key.encode('ascii', errors='ignore').decode('ascii').strip()

        text_model = settings.get('textModel', DEFAULT_OPENAI_MODEL)
        if text_model not in OPENAI_TEXT_MODELS:
            logger.warning(f"Unknown OpenAI model: {text_model}, using anyway")

        logger.info(f"OpenAI Settings: model={text_model}")

        try:
            ai_client = OpenAI(api_key=api_key)
            return self._fetch_openai_text(ai_client, text_model, text_prompt)
        except Exception as e:
            logger.error(f"Failed to make OpenAI request: {str(e)}")
            raise RuntimeError("OpenAI request failure, please check logs.")

    def _generate_with_gemini(self, settings, device_config, text_prompt):
        """Generate text using Google Gemini."""
        api_key = device_config.load_env_key("GOOGLE_GEMINI_SECRET")
        if not api_key:
            raise RuntimeError("Google Gemini API Key not configured. Add GOOGLE_GEMINI_SECRET in Settings > API Keys.")

        # Sanitize API key
        api_key = api_key.encode('ascii', errors='ignore').decode('ascii').strip()

        text_model = settings.get('geminiTextModel', DEFAULT_GEMINI_MODEL)

        logger.info(f"Gemini Settings: model={text_model}")

        try:
            from google import genai

            client = genai.Client(api_key=api_key)

            # Add randomness to prevent cached/repeated responses
            random_seed = random.randint(1, 1000000)

            # Check if prompt is asking for a joke - use special handling
            prompt_lower = text_prompt.lower()
            is_joke_request = any(word in prompt_lower for word in ["joke", "funny", "humor", "laugh", "pun"])

            if is_joke_request:
                # Random variety injectors for joke prompts
                styles = ["witty", "clever", "silly", "dry", "absurd", "punny", "observational", "surreal", "dark", "wholesome"]
                topics = ["technology", "food", "animals", "work", "relationships", "science", "history", "sports", "music", "travel"]
                random_style = random.choice(styles)
                random_topic = random.choice(topics)

                system_prompt = (
                    f"You are a {random_style} comedian who never repeats jokes. "
                    "Keep responses under 70 words. Be creative and original. "
                    "Respond directly without introductions or explanations."
                )

                # For generic joke requests, inject variety
                if len(text_prompt.split()) < 6:
                    enhanced_prompt = f"{text_prompt} (make it {random_style}, maybe about {random_topic})"
                else:
                    enhanced_prompt = text_prompt
            else:
                # General-purpose text generation
                system_prompt = (
                    "You are a helpful and creative text generation assistant. "
                    "Keep responses under 70 words. Be concise and relevant. "
                    "Respond directly without introductions or explanations."
                )
                enhanced_prompt = text_prompt

            full_prompt = f"{system_prompt}\n\nUser request: {enhanced_prompt}"
            response = client.models.generate_content(
                model=text_model,
                contents=full_prompt,
                config={
                    "temperature": 2.0,
                    "seed": random_seed
                }
            )
            result = response.text.strip()

            logger.info(f"Generated text response: {result[:100]}...")
            return result

        except ImportError:
            logger.error("google-genai package not installed")
            raise RuntimeError("Google Gemini SDK not installed. Run: pip install google-genai")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to make Gemini request: {error_msg}")

            # Provide user-friendly error messages
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                raise RuntimeError("Gemini rate limit reached. Please wait a minute and try again, or try a different model.")
            elif "API_KEY" in error_msg.upper() or "401" in error_msg:
                raise RuntimeError("Gemini API key is invalid. Please check your GOOGLE_GEMINI_SECRET in Settings > API Keys.")
            elif "404" in error_msg:
                raise RuntimeError("Gemini model not found. Please select a different model.")
            else:
                raise RuntimeError(f"Gemini error: {error_msg[:100]}")

    def _generate_with_deepseek(self, settings, device_config, text_prompt):
        """Generate text using DeepSeek via its OpenAI-compatible API.

        DeepSeek's API uses the OpenAI SDK wire format; only base_url differs.
        See: https://api-docs.deepseek.com/
        """
        from openai import OpenAI

        api_key = device_config.load_env_key("DEEPSEEK_SECRET")
        if not api_key:
            raise RuntimeError("DeepSeek API Key not configured. Add DEEPSEEK_SECRET in Settings > API Keys.")

        # Sanitize API key
        api_key = api_key.encode('ascii', errors='ignore').decode('ascii').strip()

        text_model = settings.get('deepseekTextModel', DEFAULT_DEEPSEEK_MODEL)
        if text_model not in DEEPSEEK_TEXT_MODELS:
            logger.warning(f"Unknown DeepSeek model: {text_model}, using anyway")

        logger.info(f"DeepSeek Settings: model={text_model}")

        try:
            # DeepSeek accepts the OpenAI SDK with a custom base_url.
            # Disable thinking mode for this short-text-generation use case:
            # V4 models default to thinking mode, which emits reasoning_content
            # and can leave `content` empty when the reasoning exhausts the
            # token budget. Non-thinking mode returns the answer directly in
            # `content` and is faster.
            ai_client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
            return self._fetch_openai_text(
                ai_client, text_model, text_prompt,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except RuntimeError:
            raise
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to make DeepSeek request: {error_msg}")

            if "429" in error_msg:
                raise RuntimeError("DeepSeek rate limit reached. Please wait a minute and try again.")
            elif "401" in error_msg or "API_KEY" in error_msg.upper():
                raise RuntimeError("DeepSeek API key is invalid. Please check your DEEPSEEK_SECRET in Settings > API Keys.")
            elif "404" in error_msg:
                raise RuntimeError("DeepSeek model not found. Please select a different model.")
            else:
                raise RuntimeError(f"DeepSeek error: {error_msg[:100]}")

    def _fetch_openai_text(self, ai_client, model, text_prompt, extra_body=None):
        """Fetch text response from an OpenAI-compatible chat completions API.

        Handles both OpenAI and DeepSeek. For DeepSeek V4 reasoning models the
        final answer lives in `content` while the chain-of-thought is exposed
        via `reasoning_content`; we fall back to the latter if `content` is
        empty so the display is never blank.
        """
        logger.info(f"Getting text response, model: {model}")

        system_content = (
            "You are a highly intelligent text generation assistant. Generate concise, "
            "relevant, and accurate responses tailored to the user's input. The response "
            "should be 70 words or less."
            "IMPORTANT: Do not rephrase, reword, or provide an introduction. Respond directly "
            "to the request without adding explanations or extra context "
            "IMPORTANT: If the response naturally requires a newline for formatting, provide "
            "the '\n' newline character explicitly for every new line. For regular sentences "
            "or paragraphs do not provide the new line character."
            f"For context, today is {datetime.today().strftime('%Y-%m-%d')}"
        )

        request_kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": text_prompt}
            ],
            "temperature": 1,
            # Reasoning models (e.g. DeepSeek V4 in thinking mode) can consume
            # the default token budget on chain-of-thought, leaving `content`
            # empty. 2048 comfortably covers the 70-word target plus reasoning.
            "max_tokens": 2048,
        }
        if extra_body:
            request_kwargs["extra_body"] = extra_body

        response = ai_client.chat.completions.create(**request_kwargs)

        message = response.choices[0].message
        result = (message.content or "").strip()

        if not result:
            # DeepSeek reasoning models expose the chain-of-thought via
            # reasoning_content; use it as a fallback so the display is
            # never blank.
            reasoning = getattr(message, "reasoning_content", None)
            if reasoning:
                logger.warning("Empty content, falling back to reasoning_content")
                result = reasoning.strip()

        if not result:
            raise RuntimeError("AI model returned an empty response.")

        logger.info(f"Generated text response: {result[:100]}...")
        return result
