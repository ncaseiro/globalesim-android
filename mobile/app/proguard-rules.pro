# Linphone SDK (JNI)
-keep class org.linphone.** { *; }
-keep class tel.globalesim.app.VoiceManager { *; }
# window.GlobaleSIM bridge methods are called by name from the page
-keepclassmembers class tel.globalesim.app.MainActivity$Bridge { @android.webkit.JavascriptInterface <methods>; }
-dontwarn org.linphone.**
