import SwiftUI
import UIKit

/// The web tokens of dashboard/static/atip/atip.css (UI fixes v2, 2026-11-03), light and dark. Each colour follows
/// the phone's light / dark setting by itself, as the pages do (prefers-color-scheme).
extension UIColor {
    convenience init(hex: UInt32) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }

    /// One colour for the light theme and one for the dark theme.
    convenience init(light: UInt32, dark: UInt32) {
        self.init(dynamicProvider: { $0.userInterfaceStyle == .dark ? UIColor(hex: dark) : UIColor(hex: light) })
    }

    static var axBg: UIColor { UIColor(light: 0xF6F7F9, dark: 0x0B1220) }        // --ax-bg
    static var axSurface: UIColor { UIColor(light: 0xFFFFFF, dark: 0x111A2C) }   // --ax-surface
    static var axText: UIColor { UIColor(light: 0x101828, dark: 0xE6EBF5) }      // --ax-text
    static var axMuted: UIColor { UIColor(light: 0x5D6B82, dark: 0x9AA8C1) }     // --ax-muted
    static var axBorder: UIColor { UIColor(light: 0xD0D5DD, dark: 0x2A3A57) }    // --ax-border
    static var axBrand: UIColor { UIColor(light: 0x1D4ED8, dark: 0x60A5FA) }     // --ax-brand
    static var axPos: UIColor { UIColor(light: 0x067647, dark: 0x6EE7B7) }       // --ax-pos
    static var axNeg: UIColor { UIColor(light: 0xB42318, dark: 0xFCA5A5) }       // --ax-neg
}

extension Color {
    static var axBg: Color { Color(uiColor: .axBg) }
    static var axSurface: Color { Color(uiColor: .axSurface) }
    static var axText: Color { Color(uiColor: .axText) }
    static var axMuted: Color { Color(uiColor: .axMuted) }
    static var axBorder: Color { Color(uiColor: .axBorder) }
    static var axBrand: Color { Color(uiColor: .axBrand) }
    static var axPos: Color { Color(uiColor: .axPos) }
    static var axNeg: Color { Color(uiColor: .axNeg) }
}
