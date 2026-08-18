## Tại sao

<!-- Vấn đề gì đang xảy ra, hoặc nhu cầu gì cần đáp ứng? Tránh chỉ liệt kê lại diff. -->

## Thay đổi

<!-- Tóm tắt thay đổi chính. Nếu ảnh hưởng hành vi có thể quan sát được (UI, API, export...), nói rõ trước/sau. -->

## Đã kiểm tra

- [ ] `pytest` chạy sạch
- [ ] `ruff check .` chạy sạch
- [ ] Đã build lại `frontend/styles.css` nếu có sửa `frontend/input.css`
- [ ] Đã chạy Prettier nếu có sửa file trong `frontend/`
- [ ] Đã tự test thủ công trên trình duyệt (nếu đổi UI/JS)

## Ghi chú khác

<!-- Rủi ro, giới hạn đã biết, việc cần làm tiếp theo, hoặc để trống nếu không có. -->

<!--
Merge vào main sẽ tự động build image và deploy lên VM production (xem DEPLOY.md).
Tránh merge PR chưa test kỹ hoặc còn work-in-progress.
-->
