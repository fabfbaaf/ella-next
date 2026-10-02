//! One desktop instance per Windows session, without another network service.
use std::ffi::c_void;
use std::ptr;

#[link(name = "kernel32")]
extern "system" {
    fn CreateMutexW(attributes: *const c_void, initial_owner: i32, name: *const u16)
        -> *mut c_void;
    fn GetLastError() -> u32;
    fn CloseHandle(handle: *mut c_void) -> i32;
}
#[link(name = "user32")]
extern "system" {
    fn FindWindowW(class: *const u16, title: *const u16) -> *mut c_void;
    fn ShowWindow(window: *mut c_void, command: i32) -> i32;
    fn SetForegroundWindow(window: *mut c_void) -> i32;
}
pub struct InstanceGuard(*mut c_void);
impl Drop for InstanceGuard {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}
pub fn acquire() -> Option<InstanceGuard> {
    let name = wide("Local\\ai.ella.next.desktop");
    // SAFETY: static UTF-16 buffers remain alive throughout these synchronous calls.
    let handle = unsafe { CreateMutexW(ptr::null(), 0, name.as_ptr()) };
    if handle.is_null() {
        return Some(InstanceGuard(handle));
    }
    if unsafe { GetLastError() } == 183 {
        unsafe {
            CloseHandle(handle);
        }
        for title in ["艾拉 Next 管理后台", "艾拉 Next"] {
            let title = wide(title);
            let window = unsafe { FindWindowW(ptr::null(), title.as_ptr()) };
            if !window.is_null() {
                unsafe {
                    ShowWindow(window, 9);
                    SetForegroundWindow(window);
                }
                break;
            }
        }
        None
    } else {
        Some(InstanceGuard(handle))
    }
}
